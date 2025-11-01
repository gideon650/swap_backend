# models.py
from django.db import models
from django.contrib.auth.models import User
from django.core.validators import MinValueValidator
from django.core.exceptions import ValidationError
from django.utils.translation import gettext_lazy as _
import logging
from django.core.exceptions import ValidationError
import uuid
from django.utils import timezone
from decimal import Decimal  


logger = logging.getLogger(__name__)

class BaseModel(models.Model):
    timestamp = models.DateTimeField(auto_now_add=True)

    class Meta:
        abstract = True


class AdminLog(models.Model):
    """Logs admin actions such as approvals and rejections."""
    ACTION_CHOICES = [
        ('APPROVED', 'Approved'),
        ('REJECTED', 'Rejected'),
        ('CANCELLED', 'Cancelled')
    ]
    
    admin = models.ForeignKey(User, on_delete=models.CASCADE, related_name="admin_actions")
    action = models.CharField(max_length=225, choices=ACTION_CHOICES)
    transaction_type = models.CharField(max_length=50)  # Deposit, Withdrawal, SwapRequest
    transaction_id = models.IntegerField()
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="affected_users")
    timestamp = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.admin.username} {self.action} {self.transaction_type} ID {self.transaction_id}"
    

# Updated SyntheticAsset model in your models.py

class SyntheticAsset(BaseModel):
    name = models.CharField(max_length=50)
    symbol = models.CharField(max_length=50, unique=True)
    price_usd = models.FloatField(default=0.00001)
    prev_price_usd = models.FloatField(default=0.0)
    holders = models.IntegerField(default=0)
    liquidity = models.FloatField(default=0.0)
    total_supply = models.FloatField(default=1000000.0)
    market_cap = models.CharField(max_length=50, default='$0')
    image_url = models.URLField(blank=True, null=True)
    honey_pot = models.BooleanField(default=False)
    highest_holder = models.CharField(max_length=50)
    created_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, related_name='created_assets')
    is_verified = models.BooleanField(default=False)
    price_updated_at = models.DateTimeField(auto_now=True)
    display_order = models.PositiveIntegerField(default=0, help_text="Lower numbers appear first in trending section")
    
    class Meta:
        ordering = ['display_order', '-timestamp']
    
    def save(self, *args, **kwargs):
        # Track price changes for better chart continuity
        price_changed = False
        if self.pk:  # Only if object already exists
            try:
                old = SyntheticAsset.objects.get(pk=self.pk)
                if self.price_usd != old.price_usd:
                    self.prev_price_usd = old.price_usd
                    price_changed = True
                    logger.info(f"Price changed for {self.symbol}: {old.price_usd} -> {self.price_usd}")
                    
            except SyntheticAsset.DoesNotExist:
                pass
                
        if self.price_usd < 0:
            raise ValueError("Price cannot be negative.")
            
        super().save(*args, **kwargs)
        
        # Clear ALL cache after saving if price changed
        if price_changed:
            logger.info(f"Clearing all cache for {self.symbol} due to price change")
            try:
                from django.core.cache import cache
                # Clear Django cache
                cache_patterns = [
                    f"chart_data_{self.symbol}_1min",
                    f"chart_data_{self.symbol}_5min", 
                    f"chart_data_{self.symbol}_15min",
                    f"chart_data_{self.symbol}_1hr",
                    f"last_price_{self.symbol}",
                    f"chart_data_{self.symbol}_1min_v3",
                    f"chart_data_{self.symbol}_5min_v3", 
                    f"chart_data_{self.symbol}_15min_v3",
                    f"chart_data_{self.symbol}_1hr_v3",
                ]
                for pattern in cache_patterns:
                    cache.delete(pattern)
                    logger.info(f"Cleared cache: {pattern}")
                
                # Also clear any versioned cache keys
                cache.clear()  # Clear all cache if necessary
                logger.info("Cleared all Django cache")
                
            except Exception as e:
                logger.error(f"Error clearing cache: {e}")
                
            # Also try to clear simple cache if it exists
            try:
                from .views import chart_cache
                if hasattr(chart_cache, '_cache'):
                    chart_cache._cache.clear()
                    logger.info("Cleared simple cache")
            except ImportError:
                pass
            except Exception as e:
                logger.error(f"Error clearing simple cache: {e}")

    def __str__(self):
        return f"{self.name} ({self.symbol}) - ${self.price_usd}"

    @property
    def price_change_percentage(self):
        """Calculate percentage change from previous price"""
        if self.prev_price_usd and self.prev_price_usd > 0:
            return ((self.price_usd - self.prev_price_usd) / self.prev_price_usd) * 100
        return 0.0
    
    @property
    def is_price_up(self):
        """Check if price went up"""
        return self.price_usd > self.prev_price_usd if self.prev_price_usd else False


class UserPortfolio(models.Model):
    user = models.OneToOneField(User, on_delete=models.CASCADE)
    balance_usd = models.DecimalField(max_digits=20, decimal_places=4, default=0)
    is_merchant = models.BooleanField(default=False)
    referral_code = models.CharField(max_length=10, unique=True, blank=True, null=True)
    initial_deposit_amount = models.DecimalField(max_digits=20, decimal_places=4, null=True, blank=True)
    account_number = models.CharField(max_length=16, unique=True, blank=True, null=True)
    referred_by = models.ForeignKey(User, 
                                  on_delete=models.SET_NULL, 
                                  null=True, 
                                  blank=True, 
                                  related_name='referrals_made')
    # Add balances for individual assets
    asset_a_balance = models.FloatField(default=0.0)
    asset_b_balance = models.FloatField(default=0.0)
    asset_c_balance = models.FloatField(default=0.0)
    is_frozen = models.BooleanField(default=False)
    frozen_at = models.DateTimeField(null=True, blank=True)
    frozen_reason = models.TextField(blank=True, null=True)
    frozen_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name='frozen_accounts')
    
    def save(self, *args, **kwargs):
        if not self.referral_code:
            # Generate a random 8-character alphanumeric referral code
            import random
            import string
            while True:
                code = ''.join(random.choices(
                    string.ascii_uppercase + string.digits, 
                    k=8
                ))
                if not UserPortfolio.objects.filter(referral_code=code).exists():
                    self.referral_code = code
                    break

        if not self.account_number:
            while True:
                acct_num = str(uuid.uuid4().int)[:12]
                if not UserPortfolio.objects.filter(account_number=acct_num).exists():
                    self.account_number = acct_num
                    break
        super().save(*args, **kwargs)

    def __str__(self):
        frozen_status = " (FROZEN)" if self.is_frozen else ""
        return f"{self.user.username} - ${self.balance_usd} - {self.account_number}{frozen_status}"


class Trade(models.Model):
    TRADE_TYPES = [('BUY', 'Buy'), ('SELL', 'Sell')]
    user = models.ForeignKey(User, on_delete=models.CASCADE)
    asset = models.ForeignKey(SyntheticAsset, on_delete=models.CASCADE)
    trade_type = models.CharField(max_length=4, choices=TRADE_TYPES)
    quantity = models.FloatField(validators=[MinValueValidator(0.01)])
    price_at_trade = models.FloatField()
    timestamp = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.user.username} {self.trade_type} {self.quantity} {self.asset.symbol}"
    
    

class Notification(models.Model):
    user = models.ForeignKey(User, on_delete=models.CASCADE)
    title = models.CharField(max_length=255, blank=True, null=True)
    message = models.TextField()
    is_read = models.BooleanField(default=False)
    timestamp = models.DateTimeField(auto_now_add=True)
    deposit = models.ForeignKey('Deposit', on_delete=models.CASCADE, null=True, blank=True, related_name='notifications')
    withdrawal = models.ForeignKey('Withdrawal', on_delete=models.CASCADE, null=True, blank=True)
    swap = models.ForeignKey('SwapRequest', on_delete=models.CASCADE, null=True, blank=True)
    action_buttons = models.BooleanField(default=False)  # To show/hide action buttons
    notification_sent = models.BooleanField(default=False)  # Track if push notification was sent
    firebase_message_id = models.CharField(max_length=255, null=True, blank=True)  # Store Firebase message ID

    def __str__(self):
        return f"Notification for {self.user.username} - Read: {self.is_read}"        

class FCMToken(models.Model):
    user = models.ForeignKey(User, on_delete=models.CASCADE)
    token = models.CharField(max_length=255)
    device_type = models.CharField(max_length=20, choices=[
        ('web', 'Web'),
        ('android', 'Android'),
        ('ios', 'iOS')
    ], default='web')
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=['user', 'token'],
                name='unique_user_token'
            )
        ]

    def __str__(self):
        return f"FCM Token for {self.user.username} - {self.device_type}"
        
class Transaction(BaseModel):
    STATUS_CHOICES = [
        ('PENDING', 'Pending'),
        ('APPROVED', 'Approved'),
        ('REJECTED', 'Rejected'),
    ]
    user = models.ForeignKey(User, on_delete=models.CASCADE)
    amount = models.FloatField(validators=[MinValueValidator(0.01)])
    status = models.CharField(max_length=10, choices=STATUS_CHOICES, default='PENDING')

    class Meta:
        abstract = True

    def approve(self):
        self.status = 'APPROVED'
        self.save()

    def reject(self):
        self.status = 'REJECTED'
        self.save()

# models.py - Update Withdrawal model
class Withdrawal(Transaction):
    METHOD_CHOICES = [
        ('INTERNAL', 'Internal Transfer'),
        ('BYBIT', 'Bybit Email'),
        ('ON_CHAIN', 'On-Chain'),
        ('BANK', 'Bank Transfer')
    ]
    method = models.CharField(max_length=255, choices=METHOD_CHOICES, default='INTERNAL')
    to_address = models.CharField(max_length=255, blank=True, null=True)
    chain = models.CharField(max_length=50, blank=True, null=True)  # For blockchain network selection
    merchant = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name='merchant_withdrawals')
    user_confirmation_required = models.BooleanField(default=False)
    confirmed_at = models.DateTimeField(null=True, blank=True)
    withdrawal_notes = models.TextField(blank=True, null=True)

    def clean(self):
        if self.method == 'ON_CHAIN' and not self.chain:
            raise ValidationError('Chain selection is required for on-chain withdrawals')
        if self.method == 'BANK' and not self.to_address:
            raise ValidationError('Bank details are required for fiat withdrawals')
        if self.method == 'BANK' and not self.merchant:
            raise ValidationError('Merchant selection is required for bank withdrawals')


# Update for models.py - Enhanced Deposit model
class Deposit(Transaction):
    METHOD_CHOICES = [
        ('BANK_TRANSFER', 'Bank Transfer'),
        ('BYBIT', 'Bybit Email'),
        ('ON_CHAIN', 'On-Chain'),
        ('INTERNAL', 'Internal Transfer')
    ]
    
    NETWORK_CHOICES = [
        ('TRC20', 'TRC20'),
        ('ERC20', 'ERC20'), 
        ('BSC', 'BSC'),
        ('SOL', 'SOL')
    ]
    
    method = models.CharField(max_length=255, choices=METHOD_CHOICES, default='BANK_TRANSFER')
    transaction_id = models.CharField(max_length=255, unique=True, blank=True, null=True)
    merchant = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name='merchant_deposits')
    merchant_notified_at = models.DateTimeField(null=True, blank=True)
    merchant_action_required = models.BooleanField(default=False)
    network = models.CharField(max_length=50, choices=NETWORK_CHOICES, blank=True, null=True)
    bybit_email = models.CharField(max_length=255, blank=True, null=True)
    wallet_address = models.CharField(max_length=255, blank=True, null=True)
    sender_info = models.CharField(max_length=255, blank=True, null=True) 
    deposit_notes = models.TextField(blank=True, null=True)  
    

    def clean(self):
        if self.method == 'ON_CHAIN' and not self.network:
            raise ValidationError(_('Network selection is required for on-chain deposits'))
        
        if self.method == 'ON_CHAIN' and not self.transaction_id:
            raise ValidationError(_('Transaction hash/ID is required for on-chain deposits'))
            
        if self.method == 'BANK_TRANSFER' and not self.transaction_id:
            raise ValidationError(_('Transaction ID/reference is required for bank transfers'))
            
        if self.method == 'BYBIT' and not self.bybit_email:
            raise ValidationError(_('Bybit email is required for Bybit deposits'))

    def save(self, *args, **kwargs):
        is_new = self._state.adding
        was_approved = False
        
        # Check if status is changing to APPROVED
        if self.pk:
            try:
                old_deposit = Deposit.objects.get(pk=self.pk)
                if old_deposit.status != 'APPROVED' and self.status == 'APPROVED':
                    was_approved = True
            except Deposit.DoesNotExist:
                pass
        else:
            # New deposit that's immediately approved
            if self.status == 'APPROVED':
                was_approved = True
        
        super().save(*args, **kwargs)

        # Process referral bonus when deposit is approved
        if was_approved:
            self._process_referral_bonus()

    def _process_referral_bonus(self):
        """Process referral bonus for approved deposits"""
        try:
            logger.info(f"Processing referral bonus for deposit {self.id} by user {self.user.username}")
            
            # Store initial deposit amount if not already set
            user_portfolio = UserPortfolio.objects.get(user=self.user)
            if user_portfolio.initial_deposit_amount is None:
                user_portfolio.initial_deposit_amount = self.amount
                user_portfolio.save()
                logger.info(f"Set initial deposit amount for {self.user.username}: ${self.amount}")

            # Check if this user was referred by someone
            try:
                affiliate = Affiliate.objects.get(referred_user=self.user)
                
                # Only pay bonus if this referral hasn't been paid yet
                if not affiliate.has_funded_wallet:
                    # Mark this referral as funded (PERMANENT - never reset)
                    affiliate.has_funded_wallet = True
                    affiliate.save()
                    
                    logger.info(f"{self.user.username} marked as funded referral for {affiliate.referrer.username}")

                    # Get referrer's portfolio
                    try:
                        referrer_portfolio = UserPortfolio.objects.get(user=affiliate.referrer)
                        
                        # Calculate 15% of the REFERRED USER's initial deposit (this deposit)
                        bonus_amount = Decimal(str(self.amount)) * Decimal('0.15')
                        
                        # Add bonus to referrer's balance
                        referrer_portfolio.balance_usd += bonus_amount
                        referrer_portfolio.save()

                        # Create notification for referrer
                        Notification.objects.create(
                            user=affiliate.referrer,
                            message=f"Congratulations! You've earned ${bonus_amount:.2f} bonus (15% of {self.user.username}'s first deposit of ${self.amount})!"
                        )
                        
                        logger.info(f"Paid referral bonus of ${bonus_amount:.2f} to {affiliate.referrer.username} for {self.user.username}'s first deposit of ${self.amount}")

                    except UserPortfolio.DoesNotExist:
                        logger.error(f"Portfolio not found for referrer {affiliate.referrer.id}")
                else:
                    # This user has already triggered their one-time bonus
                    logger.info(f"{self.user.username} has already funded before - no bonus paid")

            except Affiliate.DoesNotExist:
                # User wasn't referred by anyone - this is normal
                logger.debug(f"{self.user.username} was not referred by anyone")
                pass

        except UserPortfolio.DoesNotExist:
            logger.error(f"Portfolio not found for user {self.user.id}")
        except Exception as e:
            logger.error(f"Error processing referral bonus: {str(e)}", exc_info=True)


class Affiliate(models.Model):
    referrer = models.ForeignKey(User, related_name='referrals', on_delete=models.CASCADE)
    referred_user = models.OneToOneField(User, related_name='referred_by', on_delete=models.CASCADE)
    has_funded_wallet = models.BooleanField(default=False)
    timestamp = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.referrer.username} referred {self.referred_user.username}"



from django.utils import timezone
from django.core.exceptions import ValidationError
from django.utils.translation import gettext_lazy as _

class SwapRequest(models.Model):
    STATUS_CHOICES = [
        ('PENDING', 'Pending'),
        ('APPROVED', 'Approved'),
        ('IN_PROGRESS', 'In Progress'),
        ('COMPLETED', 'Completed'),
        ('CANCELLED', 'Cancelled'),
    ]
    user = models.ForeignKey(User, on_delete=models.CASCADE)
    from_asset = models.ForeignKey('SyntheticAsset', on_delete=models.CASCADE, related_name='from_swaps')
    to_asset = models.ForeignKey('SyntheticAsset', on_delete=models.CASCADE, related_name='to_swaps')
    swap_back_asset = models.ForeignKey('SyntheticAsset', on_delete=models.CASCADE, related_name='back_swaps',
                                        null=True, blank=True)
    swap_amount = models.FloatField(validators=[MinValueValidator(0.01)])
    swap_back_amount = models.FloatField(default=0.0)
    original_to_asset_price = models.DecimalField(max_digits=20, decimal_places=10, default=0.0)
    swap_time = models.DateTimeField(help_text="In user's local timezone")  # Store aware datetime
    completed_at = models.DateTimeField(null=True, blank=True, default=None)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='PENDING')

    def clean(self):
        # Ensure the swap_time is timezone-aware
        if self.swap_time and timezone.is_naive(self.swap_time):
            raise ValidationError(_('Swap time must be timezone-aware.'))

    def save(self, *args, **kwargs):
        if self.swap_time and timezone.is_naive(self.swap_time):
            # Convert to Africa/Lagos timezone
            self.swap_time = timezone.make_aware(
                self.swap_time,
                timezone.get_default_timezone()  # Will use Africa/Lagos from settings
            )
        super().save(*args, **kwargs)

    def __str__(self):
        return f"Swap {self.id} - {self.user.username} - {self.swap_amount} {self.from_asset.symbol} to {self.swap_back_asset.symbol if self.swap_back_asset else self.to_asset.symbol} at {self.swap_time}"    
    


class UserAsset(models.Model):
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="user_assets")
    asset = models.ForeignKey(SyntheticAsset, on_delete=models.CASCADE)
    balance = models.FloatField(default=0.0)

    class Meta:
        unique_together = ('user', 'asset')

    def __str__(self):
        return f"{self.user.username} - {self.asset.symbol}: {self.balance}"



class MerchantApplication(models.Model):
    STATUS_CHOICES = [
        ('PENDING', 'Pending'),
        ('APPROVED', 'Approved'),
        ('REJECTED', 'Rejected'),
    ]
    
    user = models.ForeignKey(User, on_delete=models.CASCADE)
    name = models.CharField(max_length=255)
    bank_name = models.CharField(max_length=255)
    account_number = models.CharField(max_length=50)
    status = models.CharField(max_length=10, choices=STATUS_CHOICES, default='PENDING')
    created_at = models.DateTimeField(auto_now_add=True)
    
    def __str__(self):
        return f"{self.user.username} - {self.bank_name} ({self.account_number})"


class CandlestickData(models.Model):
    """
    Master candlestick data storage - stores 1-minute intervals as base data
    All other intervals are aggregated from this data
    """
    asset = models.ForeignKey(SyntheticAsset, on_delete=models.CASCADE, related_name='candlestick_data')
    timestamp = models.DateTimeField()
    open_price = models.DecimalField(max_digits=20, decimal_places=10)
    high_price = models.DecimalField(max_digits=20, decimal_places=10) 
    low_price = models.DecimalField(max_digits=20, decimal_places=10)
    close_price = models.DecimalField(max_digits=20, decimal_places=10)
    volume = models.DecimalField(max_digits=20, decimal_places=4, default=0)  # For future use
    interval = models.CharField(max_length=10, default='1min')
    
    class Meta:
        unique_together = ['asset', 'timestamp', 'interval']
        indexes = [
            models.Index(fields=['asset', 'timestamp']),
            models.Index(fields=['asset', 'interval', 'timestamp']),
        ]
        ordering = ['timestamp']
    
    def __str__(self):
        return f"{self.asset.symbol} - {self.timestamp} - {self.interval}"

    @classmethod
    def get_latest_candle(cls, asset, interval='1min'):
        """Get the most recent candle for an asset"""
        return cls.objects.filter(asset=asset, interval=interval).order_by('-timestamp').first()
    
    @classmethod 
    def get_candles_for_period(cls, asset, start_time, end_time, interval='1min'):
        """Get candles for a specific time period"""
        return cls.objects.filter(
            asset=asset,
            interval=interval,
            timestamp__gte=start_time,
            timestamp__lte=end_time
        ).order_by('timestamp')    