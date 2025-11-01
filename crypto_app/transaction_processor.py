import logging
from django.db import transaction
from .models import Withdrawal, Deposit, UserPortfolio, Affiliate, Notification
from decimal import Decimal

logger = logging.getLogger(__name__)

class TransactionProcessor:
    """Handles deposit and withdrawal processing with proper status management."""

    def __init__(self, transaction_id):
        self.transaction_id = transaction_id

    @transaction.atomic
    def process_withdrawal(self):
        """Processes a withdrawal that has already passed balance validation"""
        try:
            withdrawal = Withdrawal.objects.select_for_update().get(
                id=self.transaction_id, 
                status='PENDING'
            )
            logger.info(f"Processing withdrawal {withdrawal.id} for user {withdrawal.user.username}")

            # Process the withdrawal (balance already verified during creation)
            withdrawal.status = 'APPROVED'
            withdrawal.save()

            logger.info(f"Withdrawal {withdrawal.id} approved successfully.")
            return {
                "status": "success",
                "message": f"Withdrawal {withdrawal.id} approved",
            }

        except Withdrawal.DoesNotExist:
            logger.error(f"Withdrawal {self.transaction_id} not found or already processed")
            return {
                "status": "error",
                "message": "Withdrawal not found or already processed"
            }
        except Exception as e:
            logger.error(
                f"Error processing withdrawal {self.transaction_id}: {str(e)}",
                exc_info=True
            )
            return {
                "status": "error",
                "message": str(e)
            }
    
    def _reject_withdrawal(self, withdrawal, reason):
        """Helper method to reject a withdrawal with proper logging."""
        withdrawal.status = 'REJECTED'
        withdrawal.save()
        
        logger.warning(f"Withdrawal {withdrawal.id} rejected. Reason: {reason}")
        return {
            "status": "rejected",
            "message": reason,
            "withdrawal_id": withdrawal.id
        }

    @transaction.atomic
    def process_deposit(self):
        """
        Processes a deposit safely with batch operations.
        NOTE: Referral bonus is now handled in Deposit.save() method in models.py
        """
        try:
            deposit = Deposit.objects.select_for_update().get(
                id=self.transaction_id, 
                status='PENDING'
            )
            logger.info(f"Processing deposit {deposit.id} for user {deposit.user.username}")

            # Approve deposit - this will trigger the save() method which handles referral bonus
            deposit.status = 'APPROVED'
            deposit.save()  # This triggers referral bonus logic in models.py

            # Update user's portfolio balance
            portfolio = UserPortfolio.objects.select_for_update().get(user=deposit.user)
            portfolio.balance_usd += Decimal(str(deposit.amount))
            portfolio.save()

            # For merchant deposits, create notification
            if deposit.method == 'BANK_TRANSFER' and deposit.merchant:
                Notification.objects.create(
                    user=deposit.user,
                    message=f"Your P2P deposit of ${deposit.amount} has been approved by {deposit.merchant.username if deposit.merchant_action_required else 'admin'}."
                )

            logger.info(f"Deposit {deposit.id} approved successfully. New balance: {portfolio.balance_usd}")
            return {
                "status": "success",
                "message": f"Deposit {deposit.id} approved",
                "new_balance": float(portfolio.balance_usd)
            }

        except Deposit.DoesNotExist:
            logger.error(f"Deposit {self.transaction_id} not found or already processed")
            return {
                "status": "error",
                "message": "Deposit not found or already processed"
            }
        except Exception as e:
            logger.error(
                f"Error processing deposit {self.transaction_id}: {str(e)}",
                exc_info=True
            )
            return {
                "status": "error",
                "message": str(e)
            }

    