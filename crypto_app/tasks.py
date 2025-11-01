from .models import *
from django.core.cache import cache
from django.db import transaction
from .transaction_processor import TransactionProcessor
import logging
from decimal import Decimal
from django.utils import timezone
from datetime import timezone as dt_timezone
import requests
import pytz
from celery import shared_task



logger = logging.getLogger(__name__)
lagos_tz = pytz.timezone('Africa/Lagos')


def process_withdrawal(withdrawal_id):
    """Process user withdrawals."""
    processor = TransactionProcessor(withdrawal_id)
    return processor.process_withdrawal()


def process_deposit(deposit_id):
    """Process user deposits."""
    processor = TransactionProcessor(deposit_id)
    return processor.process_deposit()


@transaction.atomic
def process_swap(swap_id, force_process=False):
    """Process swap using Africa/Lagos timezone consistently"""
    try:
        swap_request = SwapRequest.objects.select_for_update().get(
            id=swap_id,
            status__in=["PENDING", "APPROVED", "IN_PROGRESS"]
        )

        # Get current time in Africa/Lagos
        current_time = timezone.now().astimezone(lagos_tz)
        
        # Ensure swap time is properly in Africa/Lagos timezone
        if swap_request.swap_time.tzinfo is None:
            swap_request.swap_time = timezone.make_aware(swap_request.swap_time, lagos_tz)
        else:
            swap_request.swap_time = swap_request.swap_time.astimezone(lagos_tz)
        
        logger.info(f"Swap {swap_id} scheduled for {swap_request.swap_time} (Lagos time)")
        logger.info(f"Current time: {current_time} (Lagos time)")
        
        # Only check time if not admin-forced
        if not force_process and swap_request.swap_time > current_time:
            logger.info(f"Swap {swap_id} not due yet, {(swap_request.swap_time - current_time).total_seconds() / 60:.2f} minutes remaining")
            return f"Swap {swap_id} not due yet, scheduled for {swap_request.swap_time.strftime('%Y-%m-%d %H:%M:%S %Z')}"

        swap_request.status = "IN_PROGRESS"
        swap_request.save(update_fields=['status'])

        # Get latest asset prices
        from_asset = SyntheticAsset.objects.get(id=swap_request.from_asset.id)
        swap_back_asset = SyntheticAsset.objects.get(id=swap_request.swap_back_asset.id)
        to_asset = SyntheticAsset.objects.get(id=swap_request.to_asset.id)
        to_asset.refresh_from_db()

        # Convert all to Decimal
        from_amount = Decimal(str(swap_request.swap_amount))
        from_price = Decimal(str(from_asset.price_usd))
        back_price = Decimal(str(swap_back_asset.price_usd))
        original_to_price = Decimal(str(swap_request.original_to_asset_price))
        latest_to_price = Decimal(str(to_asset.price_usd))

        # Calculate amounts
        to_amount = from_amount * from_price / original_to_price
        swap_back_amount = to_amount * latest_to_price / back_price

        # Update swap request
        swap_request.swap_back_amount = swap_back_amount
        swap_request.status = "COMPLETED"
        swap_request.completed_at = current_time  # Use consistently the same timezone
        swap_request.save(update_fields=['swap_back_amount', 'status', 'completed_at'])

        # Update user's portfolio
        portfolio = UserPortfolio.objects.select_for_update().get(user=swap_request.user)
        portfolio.balance_usd = Decimal(str(portfolio.balance_usd)) + swap_back_amount
        portfolio.save(update_fields=['balance_usd'])

        # Create trades records
        trades = [
            Trade(user=swap_request.user, asset=from_asset, trade_type='SELL',
                  quantity=float(from_amount), price_at_trade=float(from_price)),
            Trade(user=swap_request.user, asset=to_asset, trade_type='BUY',
                  quantity=float(to_amount), price_at_trade=float(original_to_price)),
            Trade(user=swap_request.user, asset=to_asset, trade_type='SELL',
                  quantity=float(to_amount), price_at_trade=float(latest_to_price)),
            Trade(user=swap_request.user, asset=swap_back_asset, trade_type='BUY',
                  quantity=float(swap_back_amount), price_at_trade=float(back_price))
        ]
        Trade.objects.bulk_create(trades)

        # Profit/loss calculation
        profit_loss = swap_back_amount - from_amount
        profit_loss_percentage = (profit_loss / from_amount) * 100 if from_amount > 0 else 0

        notification_message = (
            f"Your swap of {from_amount} {from_asset.symbol} to {to_asset.symbol} completed at "
            f"{swap_request.completed_at.strftime('%Y-%m-%d %H:%M:%S %Z')}. "
            f"Final amount: {swap_back_amount:.6f} {swap_back_asset.symbol}. "
        )
        if profit_loss > 0:
            notification_message += f"Profit: +{profit_loss:.2f} USD (+{profit_loss_percentage:.2f}%)"
        else:
            notification_message += f"Loss: {profit_loss:.2f} USD ({profit_loss_percentage:.2f}%)"

        notifications = [
            Notification(user=swap_request.user, message=notification_message),
            Notification(user=swap_request.user,
                        message="Your portfolio balance has been updated after the swap.")
        ]
        Notification.objects.bulk_create(notifications)

        cache.delete(f"swaps_{swap_request.user.id}")
        cache.delete(f"user_portfolio_{swap_request.user.id}")

        logger.info(f"Swap {swap_id} completed successfully at {current_time.strftime('%Y-%m-%d %H:%M:%S %Z')}")
        return f"Swap {swap_id} completed successfully at {current_time.strftime('%H:%M:%S %Z')}"

    except SwapRequest.DoesNotExist:
        logger.error(f"Swap {swap_id} not found or already processed.")
        return "Swap not found"
    except Exception as e:
        logger.error(f"Error processing swap {swap_id}: {str(e)}", exc_info=True)
        if "already processed" in str(e) or "validation" in str(e).lower():
            return f"Swap failed: {str(e)}"
        raise Exception(f"Error processing swap: {str(e)}")



@shared_task
def auto_process_swaps():
    """Find and process swaps that are due for execution."""
    current_time = timezone.now().astimezone(lagos_tz)
    logger.info(f"Auto-processing swaps at {current_time}")

    # Process both PENDING and APPROVED swaps that are due
    due_swaps = SwapRequest.objects.filter(
        status__in=["PENDING", "APPROVED"], 
        swap_time__lte=current_time
    )
    
    processed_count = 0
    failed_count = 0
    
    for swap in due_swaps:
        try:
            logger.info(f"Auto-processing swap {swap.id} for user {swap.user.username}")
            result = process_swap(swap.id)
            if "completed successfully" in result.lower():
                processed_count += 1
            else:
                failed_count += 1
                logger.error(f"Swap {swap.id} processing failed: {result}")
        except Exception as e:
            failed_count += 1
            logger.error(f"Error processing swap {swap.id}: {str(e)}", exc_info=True)

    logger.info(f"Auto-process completed: {processed_count} successful, {failed_count} failed")
    return f"Auto processed {processed_count} swaps, {failed_count} failed"