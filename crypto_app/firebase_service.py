import firebase_admin
from firebase_admin import credentials, messaging
import logging
from django.conf import settings
import os
from typing import List, Dict, Any

logger = logging.getLogger(__name__)

class FirebaseService:
    _initialized = False
    
    @classmethod
    def initialize(cls):
        """Initialize Firebase Admin SDK"""
        if cls._initialized:
            return
            
        try:
            # Try to get existing app
            try:
                firebase_admin.get_app()
                logger.info("Firebase Admin SDK already initialized")
                cls._initialized = True
                return
            except ValueError:
                # App doesn't exist, need to initialize
                pass
            
            # Get service account path
            service_account_path = getattr(settings, 'FIREBASE_SERVICE_ACCOUNT_KEY', None)
            
            if not service_account_path or not os.path.exists(service_account_path):
                logger.error(f"Firebase service account key not found at: {service_account_path}")
                return
            
            # Initialize with service account
            cred = credentials.Certificate(service_account_path)
            firebase_admin.initialize_app(cred)
            
            cls._initialized = True
            logger.info("Firebase Admin SDK initialized successfully")
            
        except Exception as e:
            logger.error(f"Failed to initialize Firebase Admin SDK: {e}")
    
    @classmethod
    def send_notification(cls, token: str, title: str, body: str, data: Dict[str, str] = None) -> bool:
        """
        Send notification to a single device token
        """
        cls.initialize()
        
        if not cls._initialized:
            logger.error("Firebase not initialized")
            return False
        
        try:
            # Create message
            message = messaging.Message(
                notification=messaging.Notification(
                    title=title,
                    body=body,
                ),
                data=data or {},
                token=token,
            )
            
            # Send message
            response = messaging.send(message)
            logger.info(f"Successfully sent message: {response}")
            return True
            
        except Exception as e:
            logger.error(f"Failed to send notification: {e}")
            return False
    
    @classmethod
    def send_multicast_notification(cls, tokens: List[str], title: str, body: str, data: Dict[str, str] = None) -> Dict[str, Any]:
        """
        Send notification to multiple device tokens
        Compatible with both old and new Firebase SDK versions
        """
        cls.initialize()
        
        if not cls._initialized:
            logger.error("Firebase not initialized")
            return {"success_count": 0, "failure_count": len(tokens), "responses": []}
        
        try:
            # Create multicast message
            message = messaging.MulticastMessage(
                notification=messaging.Notification(
                    title=title,
                    body=body,
                ),
                data=data or {},
                tokens=tokens,
            )
            
            # Try new method first (Firebase SDK v6.0.0+)
            try:
                response = messaging.send_multicast(message)
                logger.info(f"Multicast message sent successfully. Success: {response.success_count}, Failure: {response.failure_count}")
                return {
                    "success_count": response.success_count,
                    "failure_count": response.failure_count,
                    "responses": response.responses
                }
            except AttributeError:
                # Fall back to older method for compatibility
                logger.info("Using legacy send method for multicast")
                
                success_count = 0
                failure_count = 0
                responses = []
                
                # Send individually if multicast not available
                for token in tokens:
                    try:
                        individual_message = messaging.Message(
                            notification=messaging.Notification(
                                title=title,
                                body=body,
                            ),
                            data=data or {},
                            token=token,
                        )
                        
                        response = messaging.send(individual_message)
                        responses.append({"success": True, "message_id": response})
                        success_count += 1
                        
                    except Exception as e:
                        responses.append({"success": False, "error": str(e)})
                        failure_count += 1
                
                logger.info(f"Legacy multicast completed. Success: {success_count}, Failure: {failure_count}")
                return {
                    "success_count": success_count,
                    "failure_count": failure_count,
                    "responses": responses
                }
                
        except Exception as e:
            logger.error(f"Failed to send multicast notification: {e}")
            return {"success_count": 0, "failure_count": len(tokens), "responses": []}
    
    @classmethod
    def send_to_topic(cls, topic: str, title: str, body: str, data: Dict[str, str] = None) -> bool:
        """
        Send notification to a topic
        """
        cls.initialize()
        
        if not cls._initialized:
            logger.error("Firebase not initialized")
            return False
        
        try:
            message = messaging.Message(
                notification=messaging.Notification(
                    title=title,
                    body=body,
                ),
                data=data or {},
                topic=topic,
            )
            
            response = messaging.send(message)
            logger.info(f"Successfully sent topic message: {response}")
            return True
            
        except Exception as e:
            logger.error(f"Failed to send topic notification: {e}")
            return False