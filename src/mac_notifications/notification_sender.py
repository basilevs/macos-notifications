from __future__ import annotations

import logging
import objc
from multiprocessing import SimpleQueue
from typing import Any

from AppKit import NSImage
from Foundation import NSDate, NSObject, NSURL, NSUserNotification, NSUserNotificationCenter
from PyObjCTools import AppHelper

from mac_notifications.notification_config import JSONNotificationConfig

logger = logging.getLogger()
logging.basicConfig(level = logging.DEBUG)

"""
This module is responsible for creating the notifications in the C-layer and listening/reporting about user activity.
"""
class NativeNotification(object):
    def cancel(self) -> None:
        pass

def send_notification(config: JSONNotificationConfig) -> NativeNotification:
    """
    Create a notification and possibly listed & report about notification activity.
    :param config: The configuration of the notification to send.
    :param queue_to_submit_events_to: The Queue to submit user activity related to the callbacks to. If this argument
    is passed, it will start the event listener after it created the Notifications. If this is None, it will only
    create the notification.
    """

    notification = NSUserNotification.alloc().init()
    notification.setIdentifier_(config.uid)
    if config is not None:
        notification.setTitle_(config.title)
    if config.subtitle is not None:
        notification.setSubtitle_(config.subtitle)
    if config.text is not None:
        notification.setInformativeText_(config.text)
    if config.icon is not None:
        url = NSURL.alloc().initWithString_(f"file://{config.icon}")
        image = NSImage.alloc().initWithContentsOfURL_(url)
        notification.setContentImage_(image)

    # Notification buttons (main action button and other button)
    if config.action_button_str:
        notification.setActionButtonTitle_(config.action_button_str)
        notification.setHasActionButton_(True)

    if config.snooze_button_str:
        notification.setOtherButtonTitle_(config.snooze_button_str)

    if config.reply_callback_present:
        notification.setHasReplyButton_(True)
        if config.reply_button_str:
            notification.setResponsePlaceholder_(config.reply_button_str)

    # Setting delivery date as current date + delay (in seconds)
    notification.setDeliveryDate_(
        NSDate.dateWithTimeInterval_sinceDate_(config.delay_in_seconds, NSDate.date())
    )

    # Schedule the notification send
    NSUserNotificationCenter.defaultUserNotificationCenter().scheduleNotification_(notification)

    class Wrapper(NativeNotification):
        def cancel(self):
            return NSUserNotificationCenter.defaultUserNotificationCenter().removeDeliveredNotification_(notification)

    # return notification
    return Wrapper()


def wait_activations(handle_activations):
    class NSUserNotificationCenterDelegate(NSObject):
        def init(self):
            self = objc.super(NSUserNotificationCenterDelegate, self).init()
            NSUserNotificationCenter.defaultUserNotificationCenter().setDelegate_(self)
            return self
        def userNotificationCenter_didDeliverNotification_(
            self, center: "_NSConcreteUserNotificationCenter", notif: "_NSConcreteUserNotification"  # type: ignore  # noqa
        ) -> None:
            """Respond to the delivering of the notification."""
            logger.debug(f"Delivered: {notif.identifier()}")

        def userNotificationCenter_didActivateNotification_(
            self, center: "_NSConcreteUserNotificationCenter", notif: "_NSConcreteUserNotification"  # type: ignore  # noqa
        ) -> None:
            """
            Respond to a user interaction with the notification.
            """
            identifier = notif.identifier()
            response = notif.response()
            activation_type = notif.activationType()

            logger.debug(f"User interacted with {identifier} with activationType {activation_type}.")
            if activation_type == 1:
                # user clicked on the notification (not on a button)
                pass

            elif activation_type == 2:  # user clicked on the action button
                handle_activations((identifier, "action_button_clicked", ""))

            elif activation_type == 3:  # User clicked on the reply button
                handle_activations((identifier, "reply_button_clicked", response.string()))

    do_not_garbage_collect_me = NSUserNotificationCenterDelegate.alloc().init()

    # Wait for the notification CallBack to happen.
    logger.debug("Started listening for user interactions with notifications.")
    AppHelper.runConsoleEventLoop()
    logger.debug("Stopped listening for user interactions")

