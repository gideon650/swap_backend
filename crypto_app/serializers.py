from rest_framework import serializers
from .models import (
    UserPortfolio, SyntheticAsset, Trade, Withdrawal, Deposit,
    Notification, Affiliate, SwapRequest, User, MerchantApplication
)
from django.core.validators import EmailValidator
from django.utils.translation import gettext_lazy as _
from django.contrib.auth.hashers import make_password


class UserSignupSerializer(serializers.Serializer):
    username = serializers.CharField(max_length=150)
    email = serializers.EmailField(validators=[EmailValidator()])
    password = serializers.CharField(write_only=True, min_length=8)
    referral_code = serializers.CharField(max_length=50, required=False, allow_blank=True)

    def validate_email(self, value):
        if User.objects.filter(email=value).exists():
            raise serializers.ValidationError(_("Email is already registered."))
        if value.endswith('@disposablemail.com'):
            raise serializers.ValidationError(_("Disposable emails are not allowed."))
        return value

    def validate_username(self, value):
        if User.objects.filter(username=value).exists():
            raise serializers.ValidationError(_("Username is already taken."))
        return value

    def create(self, validated_data):
        """Creates and returns a new user instance, given the validated data."""
        user = User.objects.create(
            username=validated_data['username'],
            email=validated_data['email'],
        )
        user.password = make_password(validated_data['password'])  # Hash the password
        user.save()

        # Handle referral code (optional)
        referral_code = validated_data.get('referral_code')
        if referral_code:
            try:
                referrer = UserPortfolio.objects.get(referral_code=referral_code)
                Affiliate.objects.create(referrer=referrer.user, referred_user=user)

            except UserPortfolio.DoesNotExist:
                # Invalid referral code. Decide if you want to log this or raise an error
                print("Invalid referral code") # Example Log

        # Create user portfolio WITH account number
        portfolio = UserPortfolio.objects.create(user=user)

        return user

class UserSerializer(serializers.ModelSerializer):
    """Serializer for User model."""
    class Meta:
        model = User
        fields = ['id', 'username', 'email']

class UserPortfolioSerializer(serializers.ModelSerializer):
    user = UserSerializer(read_only=True)
    user_id = serializers.PrimaryKeyRelatedField(source='user', queryset=User.objects.all(), write_only=True)
    account_number = serializers.CharField(read_only=True)  # Add this line

    class Meta:
        model = UserPortfolio
        fields = ['user', 'user_id', 'balance_usd', 'referral_code', 'account_number']


class SyntheticAssetSerializer(serializers.ModelSerializer):
    """Serializer for Cryptocurrency assets."""
    change = serializers.SerializerMethodField()  # <-- Add this line
    percent_change = serializers.SerializerMethodField()  # <-- Add this line
    class Meta:
        model = SyntheticAsset
        fields = '__all__'

    def get_change(self, obj):
        if obj.price_usd > obj.prev_price_usd:
            return "up"
        elif obj.price_usd < obj.prev_price_usd:
            return "down"
        else:
            return "same"   
        
    def get_percent_change(self, obj):
        try:
            if obj.prev_price_usd and obj.prev_price_usd != 0:
                percent = ((obj.price_usd - obj.prev_price_usd) / obj.prev_price_usd) * 100
                return round(percent, 2)
            else:
                return 0.0
        except Exception:
            return 0.0    

class TradeSerializer(serializers.ModelSerializer):
    """Serializer for buy/sell trades."""
    user = UserSerializer(read_only=True)
    asset = serializers.PrimaryKeyRelatedField(queryset=SyntheticAsset.objects.all())

    class Meta:
        model = Trade
        fields = ['user', 'asset', 'trade_type', 'quantity', 'price_at_trade', 'timestamp']

class WithdrawalSerializer(serializers.ModelSerializer):
    """Serializer for withdrawals."""
    user = UserSerializer(read_only=True)

    def validate_amount(self, value):
        """Ensure withdrawal amount is greater than zero."""
        if value <= 0:
            raise serializers.ValidationError("Withdrawal amount must be greater than zero.")
        return value

    class Meta:
        model = Withdrawal
        fields = ['user', 'amount', 'method', 'to_address', 'status', 'timestamp']

class DepositSerializer(serializers.ModelSerializer):
    """Serializer for deposits."""
    user = UserSerializer(read_only=True)

    def validate_amount(self, value):
        """Ensure deposit amount is greater than zero."""
        if value <= 0:
            raise serializers.ValidationError("Deposit amount must be greater than zero.")
        return value

    class Meta:
        model = Deposit
        fields = ['user', 'amount', 'method', 'transaction_id', 'network', 'status', 'timestamp', 'merchant' ]

class NotificationSerializer(serializers.ModelSerializer):
    """Serializer for user notifications."""
    user = UserSerializer(read_only=True)

    class Meta:
        model = Notification
        fields = ['user', 'message', 'is_read', 'timestamp', 'deposit', 'action_buttons']

class AffiliateSerializer(serializers.ModelSerializer):
    """Serializer for affiliate/referral relationships."""
    referrer = UserSerializer(read_only=True)
    referred_user = UserSerializer(read_only=True)

    class Meta:
        model = Affiliate
        fields = ['referrer', 'referred_user', 'has_funded_wallet', 'timestamp']

class SwapRequestSerializer(serializers.ModelSerializer):
    """Serializer for swap transactions."""
    user = UserSerializer(read_only=True)
    from_asset = serializers.PrimaryKeyRelatedField(queryset=SyntheticAsset.objects.all())
    to_asset = serializers.PrimaryKeyRelatedField(queryset=SyntheticAsset.objects.all())
    swap_back_asset = serializers.PrimaryKeyRelatedField(queryset=SyntheticAsset.objects.all())

    def validate_swap_amount(self, value):
        """Ensure swap amount is greater than zero."""
        if value <= 0:
            raise serializers.ValidationError("Swap amount must be greater than zero.")
        return value

    class Meta:
        model = SwapRequest
        fields = ['user', 'from_asset', 'to_asset', 'swap_back_asset', 'swap_amount', 'swap_back_amount', 'swap_time', 'status']


class ChangeUsernameSerializer(serializers.Serializer):
    current_password = serializers.CharField(write_only=True, required=True)
    new_username = serializers.CharField(max_length=150, required=True)

    def validate_current_password(self, value):
        user = self.context['request'].user
        if not user.check_password(value):
            raise serializers.ValidationError(_("Current password is incorrect."))
        return value

    def validate_new_username(self, value):
        user = self.context['request'].user
        if User.objects.filter(username=value).exclude(id=user.id).exists():
            raise serializers.ValidationError(_("This username is already taken."))
        return value

    def save(self):
        user = self.context['request'].user
        user.username = self.validated_data['new_username']
        user.save()
        return user


class ChangePasswordSerializer(serializers.Serializer):
    current_password = serializers.CharField(write_only=True, required=True)
    new_password = serializers.CharField(write_only=True, required=True, min_length=8)
    confirm_password = serializers.CharField(write_only=True, required=True)

    def validate_current_password(self, value):
        user = self.context['request'].user
        if not user.check_password(value):
            raise serializers.ValidationError(_("Current password is incorrect."))
        return value

    def validate(self, data):
        if data['new_password'] != data['confirm_password']:
            raise serializers.ValidationError({'confirm_password': _("Password fields didn't match.")})
        return data

    def save(self):
        user = self.context['request'].user
        user.password = make_password(self.validated_data['new_password'])
        user.save()
        return user 


class MerchantApplicationSerializer(serializers.ModelSerializer):
    class Meta:
        model = MerchantApplication
        fields = ['id', 'name', 'bank_name', 'account_number', 'status', 'created_at']
        read_only_fields = ['id', 'status', 'created_at']
        
class MerchantAccountSerializer(serializers.ModelSerializer):
    class Meta:
        model = MerchantApplication
        fields = ['id', 'name', 'bank_name', 'account_number', 'user__username']