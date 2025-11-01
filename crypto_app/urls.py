from django.urls import path
from . import views

urlpatterns = [
    path('register/', views.register_user, name='user_signup'),
    path('login/', views.user_login, name='user_login'),
    path('logout/', views.logout_user, name='user_logout'),
    path('home/', views.home, name='home'),
    path('asset/<int:asset_id>/', views.asset_detail, name='asset_detail'),
    path('candlestick/<str:symbol>/', views.candlestick_chart, name='candlestick-chart'),
    path('withdraw/', views.withdraw_funds, name='withdraw_funds'),
    path('deposit/', views.deposit_funds, name='deposit_funds'),
    path('deposit/reject/<int:deposit_id>/', views.reject_deposit, name='reject_deposit'), # New URL
    path('withdraw/reject/<int:withdrawal_id>/', views.reject_withdrawal, name='reject_withdrawal'), # New URL
    path('referral-code/', views.get_referral_code, name='get_referral_code'),
    path('trade/', views.trade_cryptocurrency, name='trade_cryptocurrency'),
    path('swap-tokens/', views.swap_tokens, name='swap_tokens'),
    path('crypto-prices/', views.get_crypto_prices, name='get_crypto_prices'),
    path('portfolio/', views.get_user_portfolio, name='get_user_portfolio'),
    path('swap/approve/<int:swap_id>/', views.complete_swap_admin, name='complete_swap_admin'),
    path('swap/cancel/<int:swap_id>/', views.cancel_swap_admin, name='cancel_swap_admin'),
    path('check-email/', views.check_email, name='check_email'),
    path('check-pending-swap/', views.check_pending_swap, name='check_pending_swap'),
    path('profile/change-username/', views.change_username, name='change_username'),
    path('profile/change-password/', views.change_password, name='change_password'),
    path('transactions/', views.get_user_transactions, name='get_user_transactions'),
    path('create-synthetic-asset/', views.create_synthetic_asset, name='create_synthetic_asset'),
    path('apply-merchant/', views.apply_merchant, name='apply_merchant'),
    path('confirm-merchant-payment/', views.confirm_merchant_payment, name='confirm_merchant_payment'),
    path('approved-merchants/', views.get_approved_merchants, name='get_approved_merchants'),
    path('notifications/', views.get_notifications, name='get_notifications'),
    path('notifications/<int:notification_id>/', views.update_notification, name='update_notification'),
    path('merchant/approve-deposit/<int:deposit_id>/', views.merchant_approve_deposit, name='merchant_approve_deposit'),
    path('merchant/decline-deposit/<int:deposit_id>/', views.merchant_decline_deposit, name='merchant_decline_deposit'),
    path('user/confirm-withdrawal/<int:withdrawal_id>/', views.user_confirm_withdrawal, name='user_confirm_withdrawal'),
    path('user/decline-withdrawal/<int:withdrawal_id>/', views.user_decline_withdrawal, name='user_decline_withdrawal'),
    path('notifications/unread-count/', views.unread_notification_count, name='unread_notification_count'),
    path('fcm/register/', views.register_fcm_token, name='register_fcm_token'),
    path('fcm/unregister/', views.unregister_fcm_token, name='unregister_fcm_token'),
    path('suspended/', views.suspended_account, name='suspended_account'),
    path('merchant-balances/', views.merchant_balances, name='merchant_balances'),
]

