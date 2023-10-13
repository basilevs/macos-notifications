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
from mac_notifications.notification_config import NotificationConfig
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


class NotificationManager(metaclass=Singleton):
    """
    The NotificationManager is responsible for managing the notifications. This includes the following:
    - Starting new notifications.
    - Starting the Callback Executor thread in the background.
    """

    def __init__(self):
        self._callback_executor_thread: CallbackExecutorThread | None = None
        self._callback_listener_process: NotificationProcess | None = None
        # Specify that once we stop our application, self.cleanup should run
        atexit.register(self.cleanup)
        # Specify that when we get a keyboard interrupt, this function should handle it
        signal.signal(signal.SIGINT, handler=self.catch_keyboard_interrupt)
        self.parent_pipe_end: Connection = None
        self.child_pipe_end: Connection = None

    def create_callback_executor_thread(self) -> None:
        """Creates the callback executor thread and sets the _callback_executor_event."""
        if not (self._callback_executor_thread and self._callback_executor_thread.is_alive()):
            self._callback_executor_thread = CallbackExecutorThread(
                callback_queue=self.parent_pipe_end,
            )
            self._callback_executor_thread.start()

    def create_notification(self, notification_config: NotificationConfig) -> None:
        """
        Create a notification and the corresponding processes if required for a notification with callbacks.
        :param notification_config: The configuration for the notification.
        """
        json_config = notification_config.to_json_notification()
        if not self._callback_listener_process:
            # We need to also start a listener, so we send the json through a separate process.
            
            self.parent_pipe_end, self.child_pipe_end = Pipe()
            self._callback_listener_process = NotificationProcess(self.child_pipe_end)
            self._callback_listener_process.start()
            self.create_callback_executor_thread()
        self.parent_pipe_end.send(json_config)

        _FIFO_LIST.append(notification_config.uid)
        _NOTIFICATION_MAP[notification_config.uid] = notification_config
        self.clear_old_notifications()

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
        if self.parent_pipe_end:
            self.parent_pipe_end.close()
            self.parent_pipe_end = None
        if self.child_pipe_end:
            self.child_pipe_end.close()
            self.child_pipe_end = None
        if self._callback_listener_process:
            self._callback_listener_process.kill()
            self._callback_listener_process = None
        if self._callback_executor_thread: 
            self._callback_executor_thread.join()
            self._callback_executor_thread = None
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
        while self.drain_queue():
            pass

    def drain_queue(self) -> None:
        """
        This drains the Callback Queue. When there is a notification for which a callback should be fired, this event is
        added to the `callback_queue`. This background Threat is then responsible for listening in on the callback_queue
        and when there is a callback it should execute, it executes it.
        """
        while True:
            try :
                if not self.callback_queue.poll(0.1):
                    break
                msg = self.callback_queue.recv()
            except EOFError:
                return False
            except OSError:
                return False
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
        return True


def clear_notification_from_existence(notification_id: str) -> None:
    """Removes all records we had of a notification"""
    if notification_id in _NOTIFICATION_MAP:
        _NOTIFICATION_MAP.pop(notification_id)
    if notification_id in _FIFO_LIST:
        _FIFO_LIST.remove(notification_id)
