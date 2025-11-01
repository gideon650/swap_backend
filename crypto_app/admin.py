import logging
import json
from django.contrib import admin, messages
from datetime import timezone
from django.utils import timezone
from django.db import transaction
from django.core.cache import cache
from django.utils.timezone import now
from django.utils.html import format_html
from django.urls import reverse
from django.utils.safestring import mark_safe
from django.db.models import Count, Sum, Q
from django.db import models 
from .models import (
    Withdrawal, Deposit, SwapRequest, Notification, AdminLog, 
    SyntheticAsset, Trade, UserPortfolio, Affiliate, User, UserAsset, MerchantApplication
)
from .tasks import process_swap
from decimal import Decimal
from .transaction_processor import TransactionProcessor 
from .notification_utils import create_and_send_notification

logger = logging.getLogger(__name__)

class BaseTransactionAdmin(admin.ModelAdmin):
    """Base admin class for transaction approvals & logging."""

    def log_admin_action(self, request, transactions, action):
        """Logs admin actions."""
        admin_username = request.user.username

        # ✅ Bulk create logs
        logs = [
            AdminLog(
                admin=request.user,
                action=action,
                transaction_type=transaction.__class__.__name__,
                transaction_id=transaction.id,
                user=transaction.user
            ) for transaction in transactions
        ]
        AdminLog.objects.bulk_create(logs)

        logger.info(f"Admin {admin_username} {action} {len(transactions)} transactions.")


# Enhanced UserPortfolio Admin
@admin.register(UserPortfolio)
class UserPortfolioAdmin(admin.ModelAdmin):
    list_display = (
        'user', 'balance_usd', 'account_number', 'referral_code','frozen_status',
        'initial_deposit_amount', 'referrals_count', 'referred_by_user', 'is_merchant_status'
    )
    list_filter = ('referred_by', 'is_merchant', 'is_frozen')
    search_fields = ('user__username', 'user__email', 'account_number', 'referral_code')
    readonly_fields = ('account_number', 'referral_code', 'referral_info')
    actions = ['toggle_merchant_status', 'freeze_accounts', 'unfreeze_accounts']
    
    fieldsets = (
        ('Portfolio Information', {
            'fields': ('user', 'balance_usd', 'account_number', 'initial_deposit_amount', 'is_merchant')
        }),
        ('Asset Balances', {
            'fields': ('asset_a_balance', 'asset_b_balance', 'asset_c_balance'),
            'classes': ('collapse',)
        }),
        ('Referral Information', {
            'fields': ('referral_code', 'referred_by', 'referral_info'),
            'classes': ('collapse',)
        }),
        ('Account Status', {
            'fields': ('is_frozen', 'frozen_at', 'frozen_reason', 'frozen_by'),
            'classes': ('collapse',)
})
    )
    
    def is_merchant_status(self, obj):
        """Display merchant status with styling"""
        if obj.is_merchant:
            return format_html('<span style="color: green; font-weight: bold;">✅ Merchant</span>')
        return format_html('<span style="color: gray;">❌ Regular User</span>')
    is_merchant_status.short_description = 'Merchant Status'
    
    def toggle_merchant_status(self, request, queryset):
        """Admin action to toggle merchant status"""
        updated = 0
        for portfolio in queryset:
            portfolio.is_merchant = not portfolio.is_merchant
            portfolio.save()
            
            status_text = "activated" if portfolio.is_merchant else "deactivated"
            Notification.objects.create(
                user=portfolio.user,
                message=f"Your merchant status has been {status_text} by admin."
            )
            updated += 1
        
        self.message_user(
            request,
            f"Updated merchant status for {updated} users",
            level=messages.SUCCESS
        )
    toggle_merchant_status.short_description = "Toggle merchant status"
    
    def get_queryset(self, request):
        return super().get_queryset(request).select_related('user', 'referred_by').prefetch_related('user__referrals')
    
    def referrals_count(self, obj):
        """Display number of referrals made by this user"""
        count = obj.user.referrals.count()
        if count > 0:
            return format_html(
                '<span style="color: green; font-weight: bold;">{}</span>',
                count
            )
        return count
    referrals_count.short_description = 'Referrals Made'
    
    def referred_by_user(self, obj):
        """Display who referred this user"""
        if obj.referred_by:
            return format_html(
                '<a href="{}" style="color: blue;">{}</a>',
                reverse('admin:auth_user_change', args=[obj.referred_by.id]),
                obj.referred_by.username
            )
        return 'Direct Sign-up'
    referred_by_user.short_description = 'Referred By'
    
    def referral_info(self, obj):
        """Display detailed referral information"""
        referrals = obj.user.referrals.all()
        funded_referrals = referrals.filter(has_funded_wallet=True)
        
        info = f"""
        <div style="padding: 10px; background: #f8f9fa; border-radius: 5px;">
            <h4>Referral Information</h4>
            <p><strong>Referral Code:</strong> {obj.referral_code}</p>
            <p><strong>Total Referrals:</strong> {referrals.count()}</p>
            <p><strong>Funded Referrals:</strong> {funded_referrals.count()}</p>
            
            {f'<p><strong>Referred By:</strong> {obj.referred_by.username}</p>' if obj.referred_by else ''}
            
            <h5>Referral List:</h5>
            <div style="max-height: 200px; overflow-y: auto;">
        """
        
        if referrals:
            for referral in referrals:
                funded_icon = "✅" if referral.has_funded_wallet else "❌"
                info += f"<p>{funded_icon} {referral.referred_user.username} ({referral.referred_user.email})</p>"
        else:
            info += "<p>No referrals yet</p>"
        
        info += "</div></div>"
        return mark_safe(info)
    referral_info.short_description = 'Referral Details'

    def frozen_status(self, obj):
        if obj.is_frozen:
            return format_html('<span style="color: red; font-weight: bold;">🔒 FROZEN</span>')
        return format_html('<span style="color: green;">✅ Active</span>')
    frozen_status.short_description = 'Account Status'

    @transaction.atomic
    def freeze_accounts(self, request, queryset):
        frozen_count = 0
        for portfolio in queryset.filter(is_frozen=False):
            portfolio.is_frozen = True
            portfolio.frozen_at = timezone.now()
            portfolio.frozen_by = request.user
            portfolio.save()
            
            Notification.objects.create(
                user=portfolio.user,
                message="Your account has been suspended. Please contact support for assistance."
            )
            frozen_count += 1
        
        self.message_user(
            request,
            f"Successfully frozen {frozen_count} accounts",
            level=messages.SUCCESS
        )

    @transaction.atomic 
    def unfreeze_accounts(self, request, queryset):
        unfrozen_count = 0
        for portfolio in queryset.filter(is_frozen=True):
            portfolio.is_frozen = False
            portfolio.frozen_at = None
            portfolio.frozen_reason = None
            portfolio.frozen_by = None
            portfolio.save()
            
            Notification.objects.create(
                user=portfolio.user,
                message="Your account has been reactivated. You can now access all features."
            )
            unfrozen_count += 1
        
        self.message_user(
            request,
            f"Successfully unfrozen {unfrozen_count} accounts", 
            level=messages.SUCCESS
        )

    freeze_accounts.short_description = "Freeze selected accounts"
    unfreeze_accounts.short_description = "Unfreeze selected accounts"

# Enhanced Affiliate Admin
@admin.register(Affiliate)
class AffiliateAdmin(admin.ModelAdmin):
    list_display = ('referrer', 'referred_user', 'has_funded_wallet', 'timestamp')
    list_filter = ('has_funded_wallet', 'timestamp')
    search_fields = ('referrer__username', 'referred_user__username', 'referrer__email', 'referred_user__email')
    #date_hierarchy = 'timestamp'
    
    def get_queryset(self, request):
        return super().get_queryset(request).select_related('referrer', 'referred_user')

@admin.register(Withdrawal)
class WithdrawalAdmin(BaseTransactionAdmin):
    list_display = ('id', 'user', 'amount', 'method',  'to_address', 'chain', 'fee_info', 'user_receives', 'total_processed', 'status', 'merchant', 'created_at', 'user_confirmation_required', 'confirmed_at')
    list_filter = ('status', 'method', 'user_confirmation_required', 'timestamp')
    search_fields = ('user__username', 'to_address', 'id')
    readonly_fields = ('created_at', 'confirmed_at', 'fee_details', 'total_processed_display', 'user_receives_display')
    actions = ['approve_withdrawals', 'reject_withdrawals', 'force_complete_withdrawals']
    list_select_related = ('user', 'merchant')

    def fee_info(self, obj):
        """Display fee information in list view"""
        if obj.withdrawal_notes:
            try:
                fee_data = json.loads(obj.withdrawal_notes)
                fee_amount = fee_data.get('fee_amount', 0) or 0
                fee_percentage = fee_data.get('fee_percentage', 0) or 0
                return f"${fee_amount:.2f} ({fee_percentage}%)"
            except:
                return "No fee"
        return "No fee"

    def user_receives(self, obj):
        """Display amount user will receive after fee"""
        if obj.withdrawal_notes:
            try:
                fee_data = json.loads(obj.withdrawal_notes)
                user_receives_amount = fee_data.get('user_receives', obj.amount)
                if user_receives_amount is None:
                    user_receives_amount = obj.amount or 0
                return f"${float(user_receives_amount):.2f}"
            except:
                return f"${float(obj.amount or 0):.2f}"
        return f"${float(obj.amount or 0):.2f}"

    def total_processed(self, obj):
        """Display total amount processed (amount + fee)"""
        if obj.withdrawal_notes:
            try:
                fee_data = json.loads(obj.withdrawal_notes)
                total_amount = fee_data.get('total_amount_processed', obj.amount)
                if total_amount is None:
                    total_amount = obj.amount or 0
                return f"${float(total_amount):.2f}"
            except:
                return f"${float(obj.amount or 0):.2f}"
        return f"${float(obj.amount or 0):.2f}"

    def fee_details(self, obj):
        """Display detailed fee information in change view"""
        if obj.withdrawal_notes:
            try:
                fee_data = json.loads(obj.withdrawal_notes)
                original_amount = float(obj.amount or 0)
                fee_percentage = float(fee_data.get('fee_percentage', 0) or 0)
                fee_amount = float(fee_data.get('fee_amount', 0) or 0)
                user_receives_amount = float(fee_data.get('user_receives', original_amount) or original_amount)
                total_processed_amount = float(fee_data.get('total_amount_processed', original_amount) or original_amount)
                
                return format_html("""
                    <div style="background: #f8f9fa; padding: 10px; border-radius: 5px;">
                        <h4>Fee Details (P2P Withdrawal)</h4>
                        <p><strong>Original Amount:</strong> ${:.2f}</p>
                        <p><strong>Fee Percentage:</strong> {:.1f}%</p>
                        <p><strong>Fee Amount:</strong> ${:.2f}</p>
                        <p><strong>User Receives:</strong> ${:.2f}</p>
                        <p><strong>Merchant Receives:</strong> ${:.2f}</p>
                    </div>
                """, 
                original_amount,
                fee_percentage,
                fee_amount,
                user_receives_amount,
                total_processed_amount)
            except Exception as e:
                return f"No fee information available (Error: {str(e)})"
        return "No fee information available"

    def total_processed_display(self, obj):
        """Display total processed amount in change view"""
        if obj.withdrawal_notes:
            try:
                fee_data = json.loads(obj.withdrawal_notes)
                total_amount = fee_data.get('total_amount_processed', obj.amount)
                if total_amount is None:
                    total_amount = obj.amount or 0
                return f"${float(total_amount):.2f}"
            except:
                return f"${float(obj.amount or 0):.2f}"
        return f"${float(obj.amount or 0):.2f}"

    def user_receives_display(self, obj):
        """Display user receives amount in change view"""
        if obj.withdrawal_notes:
            try:
                fee_data = json.loads(obj.withdrawal_notes)
                user_receives_amount = fee_data.get('user_receives', obj.amount)
                if user_receives_amount is None:
                    user_receives_amount = obj.amount or 0
                return f"${float(user_receives_amount):.2f}"
            except:
                return f"${float(obj.amount or 0):.2f}"
        return f"${float(obj.amount or 0):.2f}"
    user_receives_display.short_description = 'User Receives Amount'

    def created_at(self, obj):
        return obj.timestamp
    created_at.short_description = 'Created At'
    created_at.admin_order_field = 'timestamp'

    fieldsets = (
        ('Basic Information', {
            'fields': ('user', 'amount', 'user_receives_display', 'total_processed_display', 'fee_details', 'method', 'status', 'created_at')
        }),
        ('Recipient Details', {
            'fields': ('to_address', 'chain'),
            'classes': ('collapse',)
        }),
        ('P2P Withdrawal Details', {
            'fields': ('merchant', 'user_confirmation_required', 'confirmed_at', 'withdrawal_notes'),
            'classes': ('collapse',)
        }),
    )

    @transaction.atomic
    def approve_withdrawals(self, request, queryset):
        withdrawals = queryset.filter(status='PENDING')
        successful = []
        failed = []
        
        for withdrawal in withdrawals:
            try:
                # For P2P withdrawals, parse fee information
                if withdrawal.method == 'BANK' and withdrawal.merchant:
                    fee_info = json.loads(withdrawal.withdrawal_notes) if withdrawal.withdrawal_notes else {}
                    total_amount = Decimal(str(fee_info.get('total_amount_processed', withdrawal.amount)))
                    
                    merchant_portfolio = UserPortfolio.objects.select_for_update().get(
                        user=withdrawal.merchant
                    )
                    merchant_portfolio.balance_usd += total_amount  # Credit merchant with full amount
                    merchant_portfolio.save()
                    
                    # Create notification for merchant
                    Notification.objects.create(
                        user=withdrawal.merchant,
                        message=f"Withdrawal approved. ${total_amount:.2f} has been credited to your balance (includes 3.5% fee)."
                    )
                
                # Update withdrawal status
                withdrawal.status = 'COMPLETED'
                withdrawal.user_confirmation_required = False
                withdrawal.confirmed_at = timezone.now()
                withdrawal.save()
                
                # Create notification for user
                Notification.objects.create(
                    user=withdrawal.user,
                    message=f"Your withdrawal has been approved."
                )
                
                successful.append(withdrawal.id)
                
            except Exception as e:
                logger.error(f"Error processing withdrawal {withdrawal.id}: {str(e)}", exc_info=True)
                failed.append(withdrawal.id)
                self.message_user(
                    request,
                    f"Error processing withdrawal {withdrawal.id}: {str(e)}",
                    level=messages.ERROR
                )

        if successful:
            processed_withdrawals = withdrawals.filter(id__in=successful)
            self.log_admin_action(request, processed_withdrawals, "APPROVED")
            self.message_user(
                request,
                f"Successfully processed {len(successful)} withdrawals",
                level=messages.SUCCESS
            )
        
        if failed:
            self.message_user(
                request,
                f"Failed to process {len(failed)} withdrawals",
                level=messages.WARNING
            )

    @transaction.atomic
    def reject_withdrawals(self, request, queryset):
        withdrawals = queryset.filter(status='PENDING')
        
        for withdrawal in withdrawals:
            try:
                # Refund locked amount to user's balance
                portfolio = UserPortfolio.objects.select_for_update().get(user=withdrawal.user)
                portfolio.balance_usd += Decimal(str(withdrawal.amount))
                portfolio.save()
                
                # Update status
                withdrawal.status = 'REJECTED'
                withdrawal.user_confirmation_required = False
                withdrawal.save()
                
                # Create notification
                Notification.objects.create(
                    user=withdrawal.user,
                    message=f"Your withdrawal of ${withdrawal.amount} has been rejected. Funds have been returned to your balance."
                )
                
            except Exception as e:
                logger.error(
                    f"Error rejecting withdrawal {withdrawal.id}: {str(e)}",
                    exc_info=True
                )
                continue
        
        self.message_user(
            request,
            f"Rejected {withdrawals.count()} withdrawals and refunded balances",
            level=messages.SUCCESS
        )

    @transaction.atomic
    def force_complete_withdrawals(self, request, queryset):
        """Force complete withdrawals that are stuck in pending state"""
        withdrawals = queryset.filter(status='PENDING', user_confirmation_required=True)
        completed = []
        
        for withdrawal in withdrawals:
            try:
                # For merchant withdrawals, credit the merchant
                if withdrawal.method == 'BANK' and withdrawal.merchant:
                    merchant_portfolio = UserPortfolio.objects.select_for_update().get(
                        user=withdrawal.merchant
                    )
                    merchant_portfolio.balance_usd += Decimal(str(withdrawal.amount))
                    merchant_portfolio.save()
                    
                    Notification.objects.create(
                        user=withdrawal.merchant,
                        message=f"Admin force-completed withdrawal . ${withdrawal.amount} credited."
                    )
                
                # Update withdrawal status
                withdrawal.status = 'COMPLETED'
                withdrawal.user_confirmation_required = False
                withdrawal.confirmed_at = timezone.now()
                withdrawal.save()
                
                Notification.objects.create(
                    user=withdrawal.user,
                    message=f"Withdrawal has been approved."
                )
                
                completed.append(withdrawal.id)
                
            except Exception as e:
                logger.error(f"Error force-completing withdrawal {withdrawal.id}: {str(e)}")
                continue
        
        self.message_user(
            request,
            f"Force-completed {len(completed)} pending withdrawals",
            level=messages.SUCCESS if completed else messages.WARNING
        )

# admin.py - Updated DepositAdmin
@admin.register(Deposit)
class DepositAdmin(BaseTransactionAdmin):
    list_display = ('id', 'user', 'amount', 'fee_info', 'total_with_fee', 'method', 'network', 'transaction_id', 'status', 'timestamp', 'merchant_info')
    list_filter = ('status', 'method', 'network', 'merchant')
    search_fields = ('user__username', 'transaction_id', 'merchant__username')
    actions = ['approve_deposits', 'reject_deposits']
    readonly_fields = ('fee_details', 'total_with_fee_display', 'timestamp')
    
    def fee_info(self, obj):
        """Display fee information in list view"""
        if obj.deposit_notes:
            try:
                fee_data = json.loads(obj.deposit_notes)
                fee_amount = fee_data.get('fee_amount', 0) or 0
                fee_percentage = fee_data.get('fee_percentage', 0) or 0
                return f"${fee_amount:.2f} ({fee_percentage}%)"
            except:
                return "No fee"
        return "No fee"

    def total_with_fee(self, obj):
        """Display total amount with fee"""
        if obj.deposit_notes:
            try:
                fee_data = json.loads(obj.deposit_notes)
                total_amount = fee_data.get('total_amount_with_fee', obj.amount)
                if total_amount is None:
                    total_amount = obj.amount or 0
                return f"${float(total_amount):.2f}"
            except:
                amount = obj.amount or 0
                return f"${float(amount):.2f}"
        amount = obj.amount or 0
        return f"${float(amount):.2f}"

    def fee_details(self, obj):
        """Display detailed fee information in change view"""
        if obj.deposit_notes:
            try:
                fee_data = json.loads(obj.deposit_notes)
                base_amount = fee_data.get('base_amount', obj.amount) or obj.amount or 0
                fee_percentage = fee_data.get('fee_percentage', 0) or 0
                fee_amount = fee_data.get('fee_amount', 0) or 0
                total_amount = fee_data.get('total_amount_with_fee', obj.amount) or obj.amount or 0
                
                return format_html("""
                    <div style="background: #f8f9fa; padding: 10px; border-radius: 5px;">
                        <h4>Fee Details</h4>
                        <p><strong>Base Amount:</strong> ${:.2f}</p>
                        <p><strong>Fee Percentage:</strong> {:.1f}%</p>
                        <p><strong>Fee Amount:</strong> ${:.2f}</p>
                        <p><strong>Total with Fee:</strong> ${:.2f}</p>
                    </div>
                """, 
                float(base_amount),
                float(fee_percentage),
                float(fee_amount),
                float(total_amount))
            except Exception as e:
                return f"No fee information available (Error: {str(e)})"
        return "No fee information available"

    def total_with_fee_display(self, obj):
        """Display total with fee in change view"""
        if obj.deposit_notes:
            try:
                fee_data = json.loads(obj.deposit_notes)
                total_amount = fee_data.get('total_amount_with_fee', obj.amount)
                if total_amount is None:
                    total_amount = obj.amount or 0
                return f"${float(total_amount):.2f}"
            except:
                amount = obj.amount or 0
                return f"${float(amount):.2f}"
        amount = obj.amount or 0
        return f"${float(amount):.2f}"
    total_with_fee_display.short_description = 'Total Amount (with fee)'

    def merchant_info(self, obj):
        if obj.merchant:
            return format_html(
                '<a href="{}">{}</a> (ID: {})',
                reverse('admin:auth_user_change', args=[obj.merchant.id]),
                obj.merchant.username,
                obj.merchant.id
            )
        return "Regular Deposit (No Merchant)"
    merchant_info.short_description = 'Merchant'
    merchant_info.admin_order_field = 'merchant__username'

    fieldsets = (
        ('Basic Information', {
            'fields': ('user', 'amount', 'total_with_fee_display', 'fee_details', 'method', 'status', 'timestamp')
        }),
        ('Transaction Details', {
            'fields': ('network', 'transaction_id', 'merchant'),
            'classes': ('collapse',)
        }),
        ('Additional Information', {
            'fields': ('deposit_notes',),
            'classes': ('collapse',)
        }),
    )

    @transaction.atomic
    def approve_deposits(self, request, queryset):
        deposits = queryset.filter(status='PENDING')
        successful = []
        failed = []
        
        for deposit in deposits:
            try:
                # Parse fee information
                fee_info = json.loads(deposit.deposit_notes) if deposit.deposit_notes else {}
                base_amount = Decimal(str(fee_info.get('base_amount', deposit.amount)))
                fee_amount = Decimal(str(fee_info.get('fee_amount', 0)))
                
                # Handle merchant deposits with fee
                if deposit.method == 'BANK_TRANSFER' and deposit.merchant:
                    merchant_portfolio = UserPortfolio.objects.select_for_update().get(
                        user=deposit.merchant
                    )
                    
                    # Check merchant balance for base amount only (merchant keeps fee)
                    if merchant_portfolio.balance_usd < base_amount:
                        self.message_user(
                            request,
                            f"Merchant {deposit.merchant.username} has insufficient balance for deposit {deposit.id}. Needed: ${base_amount}, Available: ${merchant_portfolio.balance_usd}",
                            level=messages.ERROR
                        )
                        continue

                    # Deduct only base amount from merchant's balance
                    merchant_portfolio.balance_usd -= base_amount
                    merchant_portfolio.save()

                    # Notify merchant
                    Notification.objects.create(
                        user=deposit.merchant,
                        message=f"Admin approved deposit for ${base_amount}. Amount deducted from your balance. You keep ${fee_amount} fee."
                    )

                # Process the deposit (credit user with base amount)
                processor = TransactionProcessor(deposit.id)
                result = processor.process_deposit()

                if result['status'] != 'success':
                    raise Exception(result['message'])

                successful.append(deposit.id)
                
            except Exception as e:
                logger.error(f"Error processing deposit {deposit.id}: {str(e)}", exc_info=True)
                failed.append(deposit.id)
                self.message_user(
                    request,
                    f"Error processing deposit {deposit.id}: {str(e)}",
                    level=messages.ERROR
                )
                continue

        if successful:
            processed_deposits = deposits.filter(id__in=successful)
            self.log_admin_action(request, processed_deposits, "APPROVED")
            self.message_user(
                request,
                f"Successfully processed {len(successful)} deposits",
                level=messages.SUCCESS
            )
        
        if failed:
            self.message_user(
                request,
                f"Failed to process {len(failed)} deposits",
                level=messages.WARNING
            )
    
    @transaction.atomic
    def reject_deposits(self, request, queryset):
        deposits = queryset.filter(status='PENDING')
        deposits.update(status='REJECTED')
        self.log_admin_action(request, deposits, "REJECTED")
        self.message_user(request, "Deposits have been rejected.", level=messages.SUCCESS)

        
@admin.register(SwapRequest)
class SwapRequestAdmin(BaseTransactionAdmin):
    list_display = ('id', 'user', 'from_asset', 'to_asset', 'swap_back_asset', 'swap_amount', 'original_to_asset_price', 'swap_back_amount', 'swap_time', 'status')
    list_filter = ('status',)
    actions = ['complete_swap', 'cancel_swap']

    @transaction.atomic
    def complete_swap(self, request, queryset):
        """Process selected swaps immediately using Africa/Lagos timezone"""
        from django.utils import timezone
        import pytz
        
        # Get current time in Africa/Lagos
        lagos_tz = pytz.timezone('Africa/Lagos')
        now_time = timezone.now().astimezone(lagos_tz)
        
        swaps = queryset.filter(status='PENDING')
        
        if not swaps.exists():
            self.message_user(request, "No pending swaps selected.", level=messages.WARNING)
            return

        processed = []
        errors = []
        
        for swap in swaps:
            try:
                # Validate assets must be USDT
                if swap.from_asset.symbol != 'USDT' or swap.swap_back_asset.symbol != 'USDT':
                    errors.append(swap.id)
                    continue
                
                # Convert swap time to Africa/Lagos if not already
                if swap.swap_time.tzinfo is None:
                    swap.swap_time = timezone.make_aware(swap.swap_time, lagos_tz)
                else:
                    swap.swap_time = swap.swap_time.astimezone(lagos_tz)
                
                # Force process immediately (admin override)
                result = process_swap(swap_id=swap.id, force_process=True)
                
                if "completed successfully" in result.lower():
                    processed.append(swap.id)
                else:
                    errors.append(swap.id)
                    
                # Create notification
                Notification.objects.create(
                    user=swap.user,
                    message=f"Your swap has been processed at {now_time.strftime('%Y-%m-%d %H:%M:%S %Z')}."
                )
                
            except Exception as e:
                logger.error(f"Failed to process swap {swap.id}: {str(e)}", exc_info=True)
                errors.append(swap.id)

        # Build result message
        msg_parts = []
        if processed:
            msg_parts.append(f"Processed {len(processed)} swaps at {now_time.strftime('%H:%M:%S %Z')}")
        if errors:
            msg_parts.append(f"Failed {len(errors)} swaps")
            
        self.message_user(
            request,
            ", ".join(msg_parts) + ".",
            level=messages.SUCCESS if not errors else messages.WARNING
        )

    @transaction.atomic
    def cancel_swap(self, request, queryset):
        """Cancel selected swaps and refund users"""
        swaps = queryset.filter(status='PENDING')
        
        if not swaps.exists():
            self.message_user(request, "No pending swaps selected.", level=messages.WARNING)
            return
            
        for swap in swaps:
            try:
                # Refund user
                portfolio = UserPortfolio.objects.select_for_update().get(user=swap.user)
                portfolio.balance_usd += Decimal(str(swap.swap_amount))
                portfolio.save()
                
                # Update status
                swap.status = 'CANCELLED'
                swap.save()
                
                # Create notification
                Notification.objects.create(
                    user=swap.user,
                    message=f"Swap cancelled. ${swap.swap_amount} refunded."
                )
                
            except Exception as e:
                logger.error(f"Failed to cancel swap {swap.id}: {str(e)}", exc_info=True)
                continue
                
        self.log_admin_action(request, swaps, "CANCELLED")
        self.message_user(
            request,
            f"Cancelled {swaps.count()} swaps and refunded users.",
            level=messages.SUCCESS
        )

@admin.register(Notification)
class NotificationAdmin(admin.ModelAdmin):
    list_display = ('user', 'message', 'is_read', 'timestamp')
    list_filter = ('is_read',)

@admin.register(AdminLog)
class AdminLogAdmin(admin.ModelAdmin):
    list_display = ('admin', 'action', 'transaction_type', 'transaction_id', 'user', 'timestamp')
    list_filter = ('action', 'transaction_type')

# Add this to your existing SyntheticAssetAdmin in admin.py

@admin.register(SyntheticAsset)
class SyntheticAssetAdmin(admin.ModelAdmin):
    list_display = (
        'display_order', 'name', 'symbol', 'price_usd', 'holders', 
        'liquidity', 'total_supply', 'market_cap', 'timestamp', 
        'image_url', 'honey_pot', 'highest_holder', 'prev_price_usd'
    )
    list_editable = ('display_order',)  # Allow inline editing of display_order
    list_display_links = ('name',)  # Make 'name' the clickable link instead of display_order
    fields = (
        'display_order', 'name', 'symbol', 'price_usd', 'holders', 
        'liquidity', 'total_supply', 'market_cap', 'image_url', 
        'honey_pot', 'highest_holder', 'prev_price_usd'
    )
    search_fields = ('name', 'symbol')
    list_filter = ('timestamp', 'honey_pot', 'is_verified')
    ordering = ('display_order', '-timestamp')  # Order by display_order first
    save_as = True
    
    actions = ['move_to_top', 'move_to_bottom', 'reset_ordering', 'randomize_prices']
    
    def move_to_top(self, request, queryset):
        """Move selected tokens to the top of trending section"""
        for i, asset in enumerate(queryset):
            asset.display_order = i + 1
            asset.save()
        self.message_user(
            request,
            f"Moved {queryset.count()} tokens to the top of trending section",
            level=messages.SUCCESS
        )
    move_to_top.short_description = "Move selected tokens to top"
    
    def move_to_bottom(self, request, queryset):
        """Move selected tokens to the bottom of trending section"""
        max_order = SyntheticAsset.objects.aggregate(
            max_order=models.Max('display_order')
        )['max_order'] or 0
        
        for i, asset in enumerate(queryset):
            asset.display_order = max_order + i + 1
            asset.save()
        self.message_user(
            request,
            f"Moved {queryset.count()} tokens to the bottom of trending section",
            level=messages.SUCCESS
        )
    move_to_bottom.short_description = "Move selected tokens to bottom"
    
    def reset_ordering(self, request, queryset):
        """Reset ordering to creation date"""
        assets = list(SyntheticAsset.objects.all().order_by('-timestamp'))
        for i, asset in enumerate(assets):
            asset.display_order = i + 1
            asset.save()
        self.message_user(
            request,
            "Reset all token ordering to creation date order",
            level=messages.SUCCESS
        )
    reset_ordering.short_description = "Reset all tokens to creation date order"
    
    @transaction.atomic
    def randomize_prices(self, request, queryset):
        """
        Randomize asset prices with random percentage changes between -15% and +15%
        Each asset gets a different random percentage change
        """
        import random
        
        updated_assets = []
        price_changes = []
        
        try:
            # Process each asset with a random price change
            for asset in queryset:
                # Generate random percentage between -15% and +15%
                percentage_change = random.uniform(-15.0, 15.0)
                
                # Calculate new price
                old_price = float(asset.price_usd)
                price_multiplier = 1 + (percentage_change / 100)
                new_price = old_price * price_multiplier
                
                # Ensure price doesn't go below a minimum threshold (e.g., $0.00001)
                if new_price < 0.00001:
                    new_price = 0.00001
                
                # Update the asset price
                asset.price_usd = round(new_price, 8)  # Round to 8 decimal places
                asset.save()  # This will trigger the existing save logic for cache clearing
                
                # Track changes for reporting
                updated_assets.append(asset.symbol)
                price_changes.append({
                    'symbol': asset.symbol,
                    'old_price': old_price,
                    'new_price': new_price,
                    'percentage': percentage_change
                })
                
                logger.info(f"Randomized {asset.symbol}: ${old_price:.8f} -> ${new_price:.8f} ({percentage_change:+.2f}%)")
            
            # Create summary message
            increases = [pc for pc in price_changes if pc['percentage'] > 0]
            decreases = [pc for pc in price_changes if pc['percentage'] < 0]
            
            summary_lines = [
                f"Successfully randomized prices for {len(updated_assets)} assets:",
                f"• {len(increases)} assets increased in price",
                f"• {len(decreases)} assets decreased in price"
            ]
            
            # Add some specific examples
            if price_changes:
                summary_lines.append("Examples:")
                for pc in price_changes[:5]:  # Show first 5 changes
                    summary_lines.append(
                        f"• {pc['symbol']}: ${pc['old_price']:.6f} -> ${pc['new_price']:.6f} ({pc['percentage']:+.1f}%)"
                    )
                
                if len(price_changes) > 5:
                    summary_lines.append(f"... and {len(price_changes) - 5} more")
            
            self.message_user(
                request,
                "\n".join(summary_lines),
                level=messages.SUCCESS
            )
            
            # Log the action
            logger.info(f"Admin {request.user.username} randomized prices for {len(updated_assets)} assets")
            
        except Exception as e:
            logger.error(f"Error randomizing prices: {str(e)}", exc_info=True)
            self.message_user(
                request,
                f"Error occurred while randomizing prices: {str(e)}",
                level=messages.ERROR
            )
    
    randomize_prices.short_description = "🎲 Randomize prices (±15% random changes)"
    
    def get_queryset(self, request):
        """Order tokens by display_order in admin list"""
        qs = super().get_queryset(request)
        return qs.order_by('display_order', '-timestamp')
    
    # Add some helpful text in the admin
    def changelist_view(self, request, extra_context=None):
        extra_context = extra_context or {}
        extra_context['title'] = 'Manage Token Display Order - Lower numbers appear first in trending'
        return super().changelist_view(request, extra_context=extra_context)

        
    
@admin.register(Trade)
class TradeAdmin(admin.ModelAdmin):
    list_display = ('user', 'asset', 'trade_type', 'price_at_trade', 'quantity', 'timestamp')
    list_filter = ('trade_type', 'timestamp')
    search_fields = ('user__username', 'asset__symbol')
    #date_hierarchy = 'timestamp'

@admin.register(UserAsset)
class UserAssetAdmin(admin.ModelAdmin):
    list_display = ('user', 'asset', 'balance')
    list_filter = ('asset',)
    search_fields = ('user__username', 'asset__symbol')  

@admin.register(MerchantApplication)
class MerchantApplicationAdmin(admin.ModelAdmin):
    list_display = ('id', 'user', 'name', 'bank_name', 'account_number', 'status', 'created_at', 'is_user_merchant')
    list_filter = ('status', 'created_at')
    actions = ['approve_applications', 'reject_applications']
    
    def is_user_merchant(self, obj):
        """Show if user is already registered as merchant"""
        try:
            portfolio = UserPortfolio.objects.get(user=obj.user)
            if portfolio.is_merchant:
                return format_html('<span style="color: green;">✅ Yes</span>')
            else:
                return format_html('<span style="color: red;">❌ No</span>')
        except UserPortfolio.DoesNotExist:
            return format_html('<span style="color: orange;">⚠️ No Portfolio</span>')
    is_user_merchant.short_description = 'Is Merchant'
    
    @transaction.atomic
    def approve_applications(self, request, queryset):
        """Approve merchant applications"""
        approved_count = 0
        errors = []
        
        for application in queryset.filter(status='PENDING'):
            try:
                # Update application status
                application.status = 'APPROVED'
                application.save()
                
                # IMPORTANT: Register user as merchant in UserPortfolio
                portfolio, created = UserPortfolio.objects.get_or_create(
                    user=application.user,
                    defaults={'balance_usd': 0}
                )
                
                # Set merchant flag
                if not portfolio.is_merchant:
                    portfolio.is_merchant = True
                    portfolio.save()
                    
                    # Create success notification for user
                    Notification.objects.create(
                        user=application.user,
                        message=f"Congratulations! Your merchant application has been approved."
                    )
                    
                    # Create admin notification
                    Notification.objects.create(
                        user=application.user,
                        message=f"Merchant Status Activated: You are now a verified merchant with bank details: {application.bank_name} - {application.account_number}"
                    )
                    
                    approved_count += 1
                    logger.info(f"User {application.user.username} registered as merchant")
                else:
                    # User was already a merchant
                    Notification.objects.create(
                        user=application.user,
                        message="Your merchant application has been approved (merchant status was already active)."
                    )
                    
                    approved_count += 1
                    
            except Exception as e:
                error_msg = f"Error approving application {application.id}: {str(e)}"
                errors.append(error_msg)
                logger.error(error_msg, exc_info=True)
                continue
        
        # Show results to admin
        if approved_count > 0:
            self.message_user(
                request, 
                f"Successfully approved {approved_count} applications and registered users as merchants", 
                level=messages.SUCCESS
            )
        
        if errors:
            for error in errors:
                self.message_user(request, error, level=messages.ERROR)
    
    approve_applications.short_description = "Approve selected applications and register as merchants"
    
    @transaction.atomic
    def reject_applications(self, request, queryset):
        """Reject merchant applications"""
        rejected_count = 0
        
        for application in queryset.filter(status='PENDING'):
            try:
                application.status = 'REJECTED'
                application.save()
                
                # Create rejection notification
                Notification.objects.create(
                    user=application.user,
                    message=f"Your merchant application has been rejected. Please contact support if you need clarification."
                )
                
                rejected_count += 1
                
            except Exception as e:
                logger.error(f"Error rejecting application {application.id}: {str(e)}", exc_info=True)
                continue
        
        self.message_user(
            request, 
            f"Rejected {rejected_count} applications", 
            level=messages.SUCCESS if rejected_count > 0 else messages.WARNING
        )
    
    reject_applications.short_description = "Reject selected applications"