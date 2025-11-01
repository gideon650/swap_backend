from .models import Notification, FCMToken
from .firebase_service import FirebaseService
import logging

logger = logging.getLogger(__name__)

def create_and_send_notification(user, title, message, notification_type=None,
                                 deposit=None, withdrawal=None, swap=None,
                                 action_buttons=False, data=None):
    """
    Create a notification and send push notification
    """
    try:
        # Create the notification in database
        notification = Notification.objects.create(
            user=user,
            title=title,
            message=message,
            deposit=deposit,
            withdrawal=withdrawal,
            swap=swap,
            action_buttons=action_buttons
        )

        # Send push notification and get result
        success = send_push_notification(user, title, message, notification_type, data)
                
        # Mark as sent based on success
        notification.notification_sent = success
        notification.save()

        return notification

    except Exception as e:
        logger.error(f"Failed to create and send notification: {str(e)}")
        return None

def send_push_notification(user, title, message, notification_type=None, data=None):
    """
    Send push notification to all user's devices
    """
    try:
        # Get all active FCM tokens for the user
        active_tokens = FCMToken.objects.filter(
            user=user, 
            is_active=True
        )
        
        if not active_tokens.exists():
            logger.info(f"No active FCM tokens found for user {user.username}")
            return False

        tokens = [token.token for token in active_tokens]
        
        # Prepare notification data
        notification_data = data or {}
        if notification_type:
            notification_data['type'] = notification_type
                
        notification_data.update({
            'click_action': 'FLUTTER_NOTIFICATION_CLICK',
            'sound': 'default'
        })

        # Send push notification
        if len(tokens) == 1:
            # Single token
            success = FirebaseService.send_notification(
                token=tokens[0],
                title=title,
                body=message,
                data=notification_data
            )
            
            if success:
                logger.info(f"Push notification sent to user {user.username}")
                return True
            else:
                logger.warning(f"Failed to send push notification to user {user.username}")
                return False
                
        else:
            # Multiple tokens - use multicast
            result = FirebaseService.send_multicast_notification(
                tokens=tokens,
                title=title,
                body=message,
                data=notification_data
            )
            
            success_count = result.get("success_count", 0)
            failure_count = result.get("failure_count", 0)
            
            if success_count > 0:
                logger.info(f"Push notification sent to user {user.username}. Success: {success_count}, Failed: {failure_count}")
                
                # Handle failed tokens - mark them as inactive
                if failure_count > 0 and "responses" in result:
                    failed_tokens = []
                    for i, response in enumerate(result["responses"]):
                        if not response.get("success", True):
                            failed_tokens.append(tokens[i])
                    
                    if failed_tokens:
                        cleanup_invalid_tokens(user, failed_tokens)
                
                return True
            else:
                logger.warning(f"Failed to send push notification to user {user.username}. All {failure_count} attempts failed.")
                return False

    except Exception as e:
        logger.error(f"Failed to send push notification to user {user.username}: {str(e)}")
        return False

def send_push_notification_to_topic(topic, title, body, data=None):
    """
    Send push notification to a topic
    """
    try:
        success = FirebaseService.send_to_topic(
            topic=topic,
            title=title,
            body=body,
            data=data or {}
        )
        
        if success:
            logger.info(f"Push notification sent to topic {topic}")
        else:
            logger.warning(f"Failed to send push notification to topic {topic}")
            
        return success
        
    except Exception as e:
        logger.error(f"Error sending push notification to topic {topic}: {e}")
        return False

def cleanup_invalid_tokens(user, failed_tokens):
    """
    Clean up invalid FCM tokens for a user
    """
    try:
        if failed_tokens:
            # Mark specific tokens as inactive
            updated_count = FCMToken.objects.filter(
                user=user, 
                token__in=failed_tokens
            ).update(is_active=False)
            
            logger.info(f"Marked {updated_count} invalid tokens as inactive for user {user.username}")
        
    except Exception as e:
        logger.error(f"Error cleaning up tokens for user {user.username}: {e}")

def send_push_notification_to_user(user, title, body, data=None):
    """
    Simplified function to send push notification to a user
    (Kept for backward compatibility)
    """
    return send_push_notification(user, title, body, data=data)