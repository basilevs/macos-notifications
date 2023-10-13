from __future__ import annotations

import atexit
import logging
from multiprocessing.connection import Connection
import signal
import sys
import time
from multiprocessing import Pipe, SimpleQueue
from threading import Event, Thread
from typing import Dict, List

from mac_notifications.listener_process import NotificationProcess
from mac_notifications.notification_config import JSONCancelRequest, NotificationConfig
from mac_notifications.singleton import Singleton

"""
This is the module responsible for managing the notifications over time & enabling callbacks to be executed.
"""

# Once we have created more than _MAX_NUMBER_OF_CALLBACKS_TO_TRACK notifications with a callback, we remove the older
# callbacks.
_MAX_NUMBER_OF_CALLBACKS_TO_TRACK: int = 1000
# The _FIFO_LIST keeps track of the order of notifications with callbacks. This way we know what to remove after having
# more than _MAX_NUMBER_OF_CALLBACKS_TO_TRACK number of notifications with a callback.
_FIFO_LIST: List[str] = []
# The _NOTIFICATION_MAP is required to keep track of notifications that have a callback. We map the UID of the
# notification to the process that was started for it and the configuration.
# Note: _NOTIFICATION_MAP should only contain notifications with a callback!
_NOTIFICATION_MAP: Dict[str, NotificationConfig] = {}
logger = logging.getLogger()


class Resources(object):
    """ Handles all system resource that should freed up when framework is not used """
    def __init__(self):
        self.parent_pipe_end: Connection = None
        self._child_pipe_end: Connection = None
        self.parent_pipe_end, self._child_pipe_end = Pipe()
        self._callback_executor_thread: CallbackExecutorThread = CallbackExecutorThread(
                callback_queue=self.parent_pipe_end,
            )
        self._callback_executor_thread.start()
        self._callback_listener_process: NotificationProcess = NotificationProcess(self._child_pipe_end)
        self._callback_listener_process.start()
        
    def close(self) -> None:
        """Stop all processes related to the Notification callback handling."""
        self.parent_pipe_end.close()
        self._child_pipe_end.close()
        self._callback_listener_process.kill()
        self._callback_executor_thread.join()


class Notification(object):
    def cancel(self):
        pass

class NotificationManager(metaclass=Singleton):
    """
    The NotificationManager is responsible for managing the notifications. This includes the following:
    - Starting new notifications.
    - Starting the Callback Executor thread in the background.
    """

    def __init__(self):
        self._resources: Resources = None
        # Specify that once we stop our application, self.cleanup should run
        atexit.register(self.cleanup)
        # Specify that when we get a keyboard interrupt, this function should handle it
        signal.signal(signal.SIGINT, handler=self.catch_keyboard_interrupt)

    def _get_resources(self) -> Resources:
        if not self._resources:
            self._resources = Resources()
        return self._resources

    def create_notification(self, notification_config: NotificationConfig) -> None:
        """
        Create a notification and the corresponding processes if required for a notification with callbacks.
        :param notification_config: The configuration for the notification.
        """
        json_config = notification_config.to_json_notification()            
        pipe_end:Connection = self._get_resources().parent_pipe_end
        pipe_end.send(json_config)

        _FIFO_LIST.append(notification_config.uid)
        _NOTIFICATION_MAP[notification_config.uid] = notification_config
        self.clear_old_notifications()

        class NotificationClosure(Notification):
            def cancel(self):
                if not pipe_end.closed:
                    pipe_end.send(JSONCancelRequest(uid = notification_config.uid))
                clear_notification_from_existence(notification_config.uid)
        return NotificationClosure()

    @staticmethod
    def clear_old_notifications() -> None:
        """Removes old notifications when we are passed our threshold."""
        while len(_FIFO_LIST) > _MAX_NUMBER_OF_CALLBACKS_TO_TRACK:
            clear_notification_from_existence(_FIFO_LIST.pop(0))

    @staticmethod
    def get_active_running_notifications() -> int:
        """
        WARNING! This is wildly inaccurate.
        Does an attempt to get the number of active running notifications. However, if a user snoozed or deleted the
        notification, we don't get an update.
        """
        return len(_NOTIFICATION_MAP)

    def catch_keyboard_interrupt(self, *args) -> None:
        """We catch the keyboard interrupt but also pass it onto the user program."""
        self.cleanup()
        sys.exit(signal.SIGINT)

    def cleanup(self) -> None:
        """Stop all processes related to the Notification callback handling."""
        if self._resources:
            self._resources.close()
            self._resources = None
        _NOTIFICATION_MAP.clear()
        _FIFO_LIST.clear()


class CallbackExecutorThread(Thread):
    """
    Background threat that checks each 0.1 second whether there are any callbacks that it should execute.
    """

    def __init__(self, callback_queue: Connection):
        super().__init__()
        self.callback_queue: Connection = callback_queue

    def run(self) -> None:
        """
        This drains the Callback Queue. When there is a notification for which a callback should be fired, this event is
        added to the `callback_queue`. This background Threat is then responsible for listening in on the callback_queue
        and when there is a callback it should execute, it executes it.
        """
        while True:
            try :
                if not self.callback_queue.poll(0.1):
                    continue
                msg = self.callback_queue.recv()
            except OSError: # the connection is closed from Resources class
                break
            notification_uid, event_id, reply_text = msg
            if notification_uid not in _NOTIFICATION_MAP:
                logger.debug(f"Received a notification interaction for {notification_uid} which we don't know.")
                continue

            if event_id == "action_button_clicked":
                notification_config = _NOTIFICATION_MAP.pop(notification_uid)
                logger.debug(f"Executing reply callback for notification {notification_config.title}.")
                if notification_config.action_callback is None:
                    raise ValueError(f"Notifications action button pressed without callback: {notification_config}.")
                else:
                    notification_config.action_callback()
            elif event_id == "reply_button_clicked":
                notification_config = _NOTIFICATION_MAP.pop(notification_uid)
                logger.debug(f"Executing reply callback for notification {notification_config.title}, {reply_text}.")
                if notification_config.reply_callback is None:
                    raise ValueError(f"Notifications reply button pressed without callback: {notification_config}.")
                else:
                    notification_config.reply_callback(reply_text)
            else:
                raise ValueError(f"Unknown event_id: {event_id}.")
            clear_notification_from_existence(notification_uid)


def clear_notification_from_existence(notification_id: str) -> None:
    """Removes all records we had of a notification"""
    if notification_id in _NOTIFICATION_MAP:
        _NOTIFICATION_MAP.pop(notification_id)
    if notification_id in _FIFO_LIST:
        _FIFO_LIST.remove(notification_id)
