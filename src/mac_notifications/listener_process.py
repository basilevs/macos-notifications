from __future__ import annotations

from multiprocessing import Process, SimpleQueue
from multiprocessing.connection import Connection
from threading import Thread

from mac_notifications import notification_sender
from mac_notifications.notification_config import JSONCancelRequest, JSONNotificationConfig


class NotificationProcess(Process):
    """
    This is a simple process to launch a notification in a separate process.

    Why you may ask?
    First, the way we need to launch a notification using a class, this class can only be instantiated once in a
    process. Hence, for simple notifications we create a new process and then immediately stop it after the notification
    was launched.
    Second, waiting for the user interaction with a notification is a blocking operation.
    Because it is a blocking operation, if we want to be able to receive any user interaction from the notification,
    without completely halting/freezing our main process, we need to open it in a background process.
    """

    def __init__(self, connection: Connection):
        super().__init__(daemon=True)
        self.connection = connection

    def poll(self):
        try:
            while True:
                result =  self.connection.recv()
                if isinstance(result, JSONNotificationConfig):
                    notification_sender.send_notification(result)
                if isinstance(result, JSONCancelRequest):
                    notification_sender.cancel_notification(result.uid)
        except EOFError:
            pass

    def handle_activation(self, activation):
        self.connection.send(activation)

    def run(self) -> None:
        poll_thread = Thread(None, self.poll, daemon=True)
        poll_thread.start()
        notification_sender.wait_activations(self.handle_activation)
