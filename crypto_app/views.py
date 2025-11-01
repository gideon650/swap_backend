from django.http import HttpResponse
from django.shortcuts import render, get_object_or_404, redirect
from django.http import JsonResponse
from django.contrib.auth.models import User
from django.contrib.auth import login, logout
from django.views.decorators.csrf import csrf_exempt
from django.middleware.csrf import get_token
from .models import *
import json
import pytz
from django.utils import timezone
from datetime import timezone as datetime_timezone 
from datetime import datetime, timedelta
from django.db import transaction
import logging
from rest_framework.decorators import api_view, permission_classes, authentication_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework import status
from django_ratelimit.decorators import ratelimit
from .tasks import *
from .serializers import *
from django.contrib.admin.views.decorators import staff_member_required
from django.contrib import messages
from rest_framework.authentication import TokenAuthentication
import random
from decimal import Decimal, InvalidOperation
from django.utils.dateparse import parse_datetime
import math
import string
from rest_framework.permissions import AllowAny
from rest_framework.authtoken.models import Token
from .notification_utils import create_and_send_notification
from .notification_utils import send_push_notification
from .candlestick_service import CandlestickService





logger = logging.getLogger(__name__)
lagos_tz = pytz.timezone('Africa/Lagos')

# Create your views here.

# User Authentication




def error_response(message, status=400):
    """Returns a JSON error response with a warning log."""
    logger.warning(f"Error response: {message}, Status Code: {status}")
    return JsonResponse({"status": "error", "message": message}, status=status)

def success_response(message, data=None):
    """Returns a JSON success response with an optional data payload."""
    response = {"status": "success", "message": message}
    if data is not None:
        response["data"] = data
    logger.info(f"Success response: {message}, Data: {data}")
    return JsonResponse(response)




# Constants
USDT_ACCOUNT_INFO = "USDT Account Info: [Insert USDT Wallet Address Here]"  # Replace with actual info

# Decorators
def is_ajax(request):
    return request.META.get('HTTP_X_REQUESTED_WITH') == 'XMLHttpRequest'



@staff_member_required
@transaction.atomic
def complete_swap_admin(request, swap_id):
    """Allows admin to approve a swap and send it to Celery for processing."""
    swap_request = get_object_or_404(SwapRequest, id=swap_id, status="PENDING")

    try:
        # Verify assets are valid (USDT checks)
        if swap_request.from_asset.symbol != 'USDT':
            messages.error(request, f"From asset must be USDT, found {swap_request.from_asset.symbol}")
            return redirect(request.META.get("HTTP_REFERER", "/admin/"))
            
        if swap_request.swap_back_asset.symbol != 'USDT':
            messages.error(request, f"Swap back asset must be USDT, found {swap_request.swap_back_asset.symbol}")
            return redirect(request.META.get("HTTP_REFERER", "/admin/"))

        # Update status to APPROVED (not IN_PROGRESS)
        swap_request.status = "APPROVED"
        swap_request.save(update_fields=['status'])
        
      
        # Check if the swap is already due for processing
        current_time = timezone.now()
        if swap_request.swap_time <= current_time:
            # Immediate processing
            process_swap.delay(swap_request.id)
            messages.success(request, f"Swap {swap_request.id} is now being processed immediately.")
        else:
            # Schedule for future processing
            messages.success(request, 
                            f"Swap {swap_request.id} approved and scheduled for processing at {swap_request.swap_time.strftime('%Y-%m-%d %H:%M:%S')}")
            
        
    except Exception as e:
        logger.error(f"Error approving swap {swap_request.id}: {str(e)}", exc_info=True)
        messages.error(request, f"Error approving swap {swap_request.id}: {str(e)}")


@staff_member_required
@transaction.atomic
def cancel_swap_admin(request, swap_id):
    """Allows admin to cancel a swap request and refund the user."""
    swap_request = get_object_or_404(SwapRequest, id=swap_id, status="PENDING")

    try:
        swap_request.status = "CANCELLED"
        swap_request.save(update_fields=['status'])
        
        # Refund the locked amount to the user's balance
        portfolio = UserPortfolio.objects.select_for_update().get(user=swap_request.user)
        portfolio.balance_usd += Decimal(str(swap_request.swap_amount))
        portfolio.save(update_fields=['balance_usd'])
        
       
        

        messages.success(request, f"Swap {swap_request.id} has been cancelled and funds returned to user.")
    except Exception as e:
        logger.error(f"Error cancelling swap {swap_request.id}: {str(e)}", exc_info=True)
        messages.error(request, f"Error cancelling swap {swap_request.id}: {str(e)}")

    return redirect(request.META.get("HTTP_REFERER", "/admin/"))


@staff_member_required
@transaction.atomic
def reject_deposit(request, deposit_id):
    """Allows admin to reject a deposit."""
    try:
        deposit = Deposit.objects.get(id=deposit_id, status="PENDING")
        deposit.status = "REJECTED"
        deposit.save()

        

        return Response({"message": f"Deposit {deposit_id} has been rejected."})

    except Deposit.DoesNotExist:
        logger.error(f"Deposit {deposit_id} not found or already processed.")
        return Response({"error": "Deposit not found or already processed."}, status=404)

    except Exception as e:
        logger.error(f"Error rejecting deposit {deposit_id}: {str(e)}", exc_info=True)
        return Response({"error": "An error occurred while rejecting the deposit."}, status=500)

@staff_member_required
@transaction.atomic
def reject_withdrawal(request, withdrawal_id):
    """Allows admin to reject a withdrawal."""
    try:
        withdrawal = Withdrawal.objects.get(id=withdrawal_id, status="PENDING")
        withdrawal.status = "REJECTED"
        withdrawal.save()  # Corrected line
        
        return Response({"message": f"Withdrawal {withdrawal_id} has been rejected."})
    except Withdrawal.DoesNotExist:
        logger.error(f"Withdrawal {withdrawal_id} not found or already processed.")
        return Response({"error": "Withdrawal not found or already processed."}, status=404)
    except Exception as e:
        logger.error(f"Error rejecting withdrawal {withdrawal_id}: {str(e)}", exc_info=True)
        return Response({"error": "An error occurred while rejecting the withdrawal."}, status=500)




@csrf_exempt
@api_view(['POST'])
@permission_classes([AllowAny])
def register_user(request):
    serializer = UserSignupSerializer(data=request.data)
    if serializer.is_valid():
        try:
            with transaction.atomic():
                user = serializer.save()
                
                # Create or get token for immediate login
                token, created = Token.objects.get_or_create(user=user)
                
                # Create portfolio
                portfolio, created = UserPortfolio.objects.get_or_create(
                    user=user,
                    defaults={
                        'balance_usd': 0,
                        'referral_code': ''.join(random.choices(string.ascii_uppercase + string.digits, k=8))
                    }
                )
                
                # Handle referral code - FIXED VERSION
                referral_code = request.data.get('referral_code')
                if referral_code:
                    try:
                        referrer_portfolio = UserPortfolio.objects.get(
                            referral_code=referral_code
                        )
                        # Create Affiliate record only if it doesn't exist
                        affiliate, created = Affiliate.objects.get_or_create(
                            referrer=referrer_portfolio.user,
                            referred_user=user,
                            defaults={'has_funded_wallet': False}
                        )
                        # Update user's portfolio with referrer
                        portfolio.referred_by = referrer_portfolio.user
                        portfolio.save()
                        
                        # Create notification for referrer
                        Notification.objects.create(
                            user=referrer_portfolio.user,
                            message=f"New user {user.username} has registered using your referral code!"
                        )
                        
                        logger.info(f"Referral relationship created: {referrer_portfolio.user.username} -> {user.username}")
                        
                    except UserPortfolio.DoesNotExist:
                        logger.warning(f"Invalid referral code used: {referral_code}")
                        # Don't return error, just continue without referral
                
                return Response({
                    "message": "Registration successful",
                    "token": token.key,
                    "user_id": user.id,
                    "username": user.username,
                    "account_number": portfolio.account_number
                }, status=status.HTTP_201_CREATED)
                
        except Exception as e:
            logger.error(f"Registration error: {str(e)}", exc_info=True)
            return Response({
                "error": "Registration failed. Please try again."
            }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)
    
    return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

    
def get_csrf_token(request):
    """Retrieve and return the CSRF token."""
    csrf_token = get_token(request)
    return JsonResponse({'csrfToken': csrf_token})
  




@api_view(['GET'])
def check_email(request):
    """
    Check if an email address is already registered.
    """
    email = request.GET.get('email')
    if not email:
        return Response({'error': 'Email parameter is required.'}, status=400)

    exists = User.objects.filter(email=email).exists()
    return Response({'exists': exists})



@csrf_exempt
@ratelimit(key='ip', rate='5/m', block=True)
@api_view(['POST'])
def user_login(request):
    """Login the user and return user info."""
    try:
        identifier = request.data.get("username")
        password = request.data.get("password")
        
        if not identifier or not password:
            logger.warning("Login attempt missing username/email or password.")
            return Response({
                "error": "Username/Email and password are required."
            }, status=status.HTTP_400_BAD_REQUEST)
        
        # Try to authenticate using username first, then email
        user = User.objects.filter(username=identifier).first() or User.objects.filter(email=identifier).first()
        
        if user and user.check_password(password):
            login(request, user)
            
            # CONSISTENT TOKEN METHOD - same as registration
            token, created = Token.objects.get_or_create(user=user)
            
            logger.info(f"User {user.username} logged in successfully.")
            return Response({
                "message": "Login successful",
                "username": user.username,
                "email": user.email,
                "token": token.key  # Same token type as registration
            })
        else:
            logger.warning(f"Invalid login attempt for username/email: {identifier}")
            return Response({
                "error": "Invalid username/email or password."
            }, status=status.HTTP_401_UNAUTHORIZED)
    
    except Exception as e:
        logger.error(f"Login error: {str(e)}")
        
        if "Too many requests" in str(e):
            return Response({
                "error": "Too many login attempts. Please try again later."
            }, status=status.HTTP_429_TOO_MANY_REQUESTS)
        
        return Response({
            "error": "An unexpected error occurred."
        }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)
    

@api_view(['POST'])
@permission_classes([IsAuthenticated])
def logout_user(request):
    """
    Logs the user out and returns a success response.
    """
    try:
        # Log out the user by clearing the session
        logout(request)

        # Return success response after logout
        return JsonResponse({"status": "success", "message": "User logged out successfully."})

    except Exception as e:
        # Log any unexpected errors
        logger.error(f"Error logging out user: {str(e)}", exc_info=True)

        # Return a failure response
        return JsonResponse({"status": "error", "message": "An error occurred while logging out."}, status=500) 



@api_view(['GET'])
@permission_classes([IsAuthenticated])
def home(request):
    user = request.user
    portfolio = get_object_or_404(UserPortfolio, user=user)

    # Get total asset value (USD)
    total_assets = portfolio.balance_usd  # Assuming portfolio.balance_usd is the total value

    # Get transaction history (last 5 trades, deposits, withdrawals)
    trades = list(Trade.objects.filter(user=user).order_by('-timestamp')[:5].values())
    deposits = list(Deposit.objects.filter(user=user).order_by('-timestamp')[:5].values())
    withdrawals = list(Withdrawal.objects.filter(user=user).order_by('-timestamp')[:5].values())

    
    # Prepare response data
    response_data = {
        'user': {
            'id': user.id,
            'username': user.username,
            'email': user.email
        },
        'total_assets': total_assets,
        'trades': trades,
        'deposits': deposits,
        'withdrawals': withdrawals,
    }

    return JsonResponse(response_data)


@api_view(['GET'])
@authentication_classes([TokenAuthentication])
@permission_classes([IsAuthenticated])
def asset_detail(request, asset_id):
    asset = get_object_or_404(SyntheticAsset, id=asset_id)
    return render(request, 'asset_detail.html', {'asset': asset})

    

# Modified candlestick view using simple cache
@api_view(['GET']) 
@permission_classes([IsAuthenticated]) 
def candlestick_chart(request, symbol):     
    """     
    Enhanced candlestick chart data endpoint - NO CACHING - ALWAYS FRESH DATA
    """     
    try:         
        logger.info(f"Received candlestick request for symbol: {symbol}")         
        logger.info(f"Request URL: {request.get_full_path()}")         
        logger.info(f"Request GET params: {dict(request.GET)}")                  
        
        # Get the asset         
        try:             
            asset = SyntheticAsset.objects.get(symbol=symbol)             
            logger.info(f"Asset found: {asset.name} ({asset.symbol}) - Current Price: ${asset.price_usd}")         
        except SyntheticAsset.DoesNotExist:             
            logger.error(f"No asset found with symbol: {symbol}")             
            return Response({                 
                "status": "error",                  
                "message": f"Asset with symbol {symbol} not found"             
            }, status=404)          
        
        # Get interval parameter         
        interval = request.GET.get('interval', '15min').lower()         
        logger.info(f"Using interval: {interval}")                  
        
        # Validate interval         
        valid_intervals = ['1min', '5min', '15min', '1hr', '1hour']         
        if interval not in valid_intervals:             
            logger.error(f"Invalid interval: {interval}")             
            return Response({                 
                "status": "error",                 
                "message": f"Invalid interval. Must be one of: {', '.join(valid_intervals)}"             
            }, status=400)                  
        
        # NO CACHING - Always get fresh data from service         
        logger.info(f"Getting fresh chart data for {symbol} - {interval}")         
        chart_data = CandlestickService.get_chart_data(asset, interval)                  
        
        logger.info(f"Service returned {len(chart_data) if chart_data else 0} candles")                  
        
        if not chart_data:             
            logger.warning(f"No chart data generated for {symbol}")                          
            
            # Try to get basic data count for debugging             
            total_candles = CandlestickData.objects.filter(asset=asset).count()             
            minute_candles = CandlestickData.objects.filter(asset=asset, interval='1min').count()                          
            
            logger.info(f"Debug info - Total candles: {total_candles}, 1min candles: {minute_candles}")                          
            
            # Return empty chart data instead of 404             
            return Response({                 
                "status": "success",                 
                "chart": [],                 
                "message": "No chart data available yet, please try again in a moment"             
            })                  
        
        # Ensure the chart data reflects the current asset price
        if chart_data and hasattr(asset, 'price_usd') and asset.price_usd:
            current_price = float(asset.price_usd)
            if current_price > 0:
                # Double-check the last candle reflects current price
                last_candle = chart_data[-1]
                if abs(last_candle['close'] - current_price) / current_price > 0.1:  # 10% difference
                    logger.info(f"Adjusting last candle close price from {last_candle['close']} to {current_price}")
                    last_candle['close'] = current_price
                    last_candle['high'] = max(last_candle['high'], current_price)
                    last_candle['low'] = min(last_candle['low'], current_price)
        
        logger.info(f"Successfully generated {len(chart_data)} fresh candles for {symbol} - {interval}")                  
        
        return Response({             
            "status": "success",              
            "chart": chart_data,
            "current_price": float(asset.price_usd) if asset.price_usd else None,
            "last_updated": timezone.now().isoformat()
        })              
        
    except Exception as e:         
        logger.error(f"Error in candlestick_chart for {symbol}: {e}", exc_info=True)         
        return Response({             
            "status": "error",             
            "message": "Failed to generate chart data"         
        }, status=500)
    

# views.py updates
import requests  # Add this import at the top of your views.py

# views.py - Updated deposit_funds function
@api_view(['POST'])
@authentication_classes([TokenAuthentication])
@permission_classes([IsAuthenticated])
def deposit_funds(request):
    """Handles user deposits - when user sends money to merchant's bank account"""
    try:
        # Check if user account is frozen
        if hasattr(request.user, 'userportfolio') and request.user.userportfolio.is_frozen:
            return JsonResponse({
                "status": "error", 
                "message": "Account is suspended. Cannot process transactions."
            }, status=403)
            
        data = json.loads(request.body)
        print(f"Received deposit data: {data}")  # Debug print
        
        # Validate required fields
        required_fields = ['amount', 'method']
        for field in required_fields:
            if field not in data:
                print(f"Missing required field: {field}")  # Debug print
                return JsonResponse({
                    "status": "error",
                    "message": f"Missing required field: {field}"
                }, status=400)
        
        # Get current USD to NGN rate from CoinGecko
        try:
            response = requests.get(
                'https://api.coingecko.com/api/v3/simple/price?ids=tether&vs_currencies=ngn',
                timeout=5  # 5 second timeout
            )
            response.raise_for_status()  # Raise exception for bad status codes
            rate_data = response.json()
            usd_to_ngn = rate_data['tether']['ngn']
        except (requests.RequestException, KeyError) as e:
            logger.error(f"Failed to fetch USD to NGN rate: {str(e)}")
            # Fallback to a default rate if API fails
            usd_to_ngn = 1600
        
        # For P2P deposits (user sending money to merchant's bank account)
        if data.get('method') == 'BANK_TRANSFER':
            print("Processing BANK_TRANSFER deposit")  # Debug print
            if not data.get('merchant_id'):
                print("Missing merchant_id for BANK_TRANSFER")  # Debug print
                return JsonResponse({
                    "status": "error",
                    "message": "Merchant ID is required for bank transfers"
                }, status=400)
            
            try:
                # Get merchant application instead of user directly
                merchant_app = MerchantApplication.objects.get(
                    id=data['merchant_id'],
                    status='APPROVED'
                )
                merchant_user = merchant_app.user
                print(f"Found merchant: {merchant_user.username}")  # Debug print
                
                # Verify merchant status through portfolio
                merchant_portfolio = UserPortfolio.objects.get(user=merchant_user)
                if not merchant_portfolio.is_merchant:
                    return JsonResponse({
                        "status": "error",
                        "message": "Selected merchant is not active"
                    }, status=400)
                
                # Calculate amount with 3.5% fee
                base_amount = Decimal(str(data['amount']))
                fee_amount = base_amount * Decimal('0.035')
                total_amount_with_fee = base_amount + fee_amount
                
                # Create the deposit record with the original amount (without fee)
                deposit = Deposit.objects.create(
                    user=request.user,
                    amount=base_amount,  # Store the base amount without fee
                    method=data['method'],
                    network=data.get('network', ''),
                    transaction_id=data.get('transaction_id', ''),
                    status='PENDING',
                    merchant=merchant_user,  # Store the user reference
                    merchant_action_required=True,
                    # Store fee information for tracking
                    deposit_notes=json.dumps({
                        'fee_percentage': 3.5,
                        'fee_amount': float(fee_amount),
                        'total_amount_with_fee': float(total_amount_with_fee),
                        'base_amount': float(base_amount)
                    })
                )
                
                # Calculate Naira amount using current rate (with fee included)
                naira_amount = total_amount_with_fee * Decimal(str(usd_to_ngn))
                
                # Notifications - Fixed: Use deposit foreign key instead of deposit_id
                create_and_send_notification(
                    user=merchant_user,
                    title="Deposit Submitted",
                    message=f"Hello {merchant_user.username}. An inward transaction was just initiated username - {request.user.username}, transaction ID - {deposit.transaction_id or 'N/A'}, amount - ₦{naira_amount:.2f} (${total_amount_with_fee:.2f} including 3.5% fee)",
                    deposit=deposit,  # Use the foreign key field, not deposit_id
                    action_buttons=True,
                )
                create_and_send_notification(
                    user=request.user,
                    title="Deposit Submitted",
                    message=f"Deposit request sent to {merchant_user.username}. Total amount: ${total_amount_with_fee:.2f} (includes 3.5% fee)"
                )
                
                return JsonResponse({
                    "status": "success",
                    "message": f"Deposit will be processed within 24 hours",
                    "deposit_id": deposit.id,
                    "merchant_name": merchant_user.username,
                    "exchange_rate": float(usd_to_ngn),
                    "total_amount_with_fee": float(total_amount_with_fee),
                    "base_amount": float(base_amount),
                    "fee_amount": float(fee_amount)
                })
                
            except MerchantApplication.DoesNotExist:
                return JsonResponse({
                    "status": "error",
                    "message": "Approved merchant not found"
                }, status=404)
            except UserPortfolio.DoesNotExist:
                return JsonResponse({
                    "status": "error",
                    "message": "Merchant profile not found"
                }, status=404)
        
        # For regular deposits (not P2P)
        else:
            deposit = Deposit.objects.create(
                user=request.user,
                amount=Decimal(str(data['amount'])),
                method=data['method'],
                network=data.get('network', ''),
                transaction_id=data.get('transaction_id', ''),
                status='PENDING',
                merchant=None,
                merchant_action_required=False
            )
            
            return JsonResponse({
                "status": "success",
                "message": f"Deposit will be processed within 24 hours.",
                "deposit_id": deposit.id,
                "exchange_rate": float(usd_to_ngn)  # Optional: return the rate used
            })
            
    except json.JSONDecodeError:
        return JsonResponse({
            "status": "error",
            "message": "Invalid JSON data"
        }, status=400)
    except Exception as e:
        print(f"Deposit processing error: {str(e)}")
        return JsonResponse({
            "status": "error",
            "message": "An error occurred processing your deposit"
        }, status=500)


@api_view(['POST'])
@authentication_classes([TokenAuthentication])
@permission_classes([IsAuthenticated])
def withdraw_funds(request):
    try:
        form_data = json.loads(request.body)
        user = request.user

        # Check if user account is frozen
        if hasattr(user, 'userportfolio') and user.userportfolio.is_frozen:
            return JsonResponse({
                'status': 'error', 
                'message': 'Account is suspended. Cannot process transactions.'
            }, status=403)

        # Validate required fields
        if not all([form_data.get('amount'), form_data.get('method')]):
            return JsonResponse({'status': 'error', 'message': 'Missing required fields'}, status=400)

        amount = Decimal(str(form_data['amount']))
        if amount <= 0:
            return JsonResponse({'status': 'error', 'message': 'Amount must be greater than 0'}, status=400)

        with transaction.atomic():
            sender_portfolio = UserPortfolio.objects.select_for_update().get(user=user)
            if sender_portfolio.balance_usd < amount:
                return JsonResponse({'status': 'error', 'message': 'Insufficient balance'}, status=400)

            # --- Bank Transfer (P2P) Logic ---
            if form_data['method'] == 'BANK':
                if not all([form_data.get('account_name'), form_data.get('account_number'), form_data.get('bank_name'), form_data.get('merchant_id')]):
                    return JsonResponse({'status': 'error', 'message': 'All bank details and merchant selection are required'}, status=400)

                try:
                    # First, get the MerchantApplication to verify it's approved
                    merchant_application = MerchantApplication.objects.get(
                        id=form_data['merchant_id'],
                        status='APPROVED'
                    )
                    
                    # Then get the associated user
                    merchant = merchant_application.user
                    
                    # Verify the user has merchant privileges
                    merchant_portfolio = UserPortfolio.objects.get(user=merchant)
                    if not merchant_portfolio.is_merchant:
                        # If they don't have merchant status but are approved, set it
                        merchant_portfolio.is_merchant = True
                        merchant_portfolio.save()
                        
                except MerchantApplication.DoesNotExist:
                    return JsonResponse({'status': 'error', 'message': 'Selected merchant not found or not approved'}, status=404)
                except UserPortfolio.DoesNotExist:
                    return JsonResponse({'status': 'error', 'message': 'Merchant portfolio not found'}, status=404)

                # Calculate 3.5% fee and net amount user will receive
                fee_amount = amount * Decimal('0.05')
                user_receives = amount - fee_amount

                # Create withdrawal with PENDING status and store fee information
                withdrawal = Withdrawal(
                    user=user,
                    amount=amount,  # Store the original amount
                    method='BANK',
                    status='PENDING',
                    merchant=merchant,
                    user_confirmation_required=True,
                    to_address=json.dumps({
                        'account_name': form_data['account_name'],
                        'account_number': form_data['account_number'],
                        'bank_name': form_data['bank_name']
                    }),
                    withdrawal_notes=json.dumps({
                        'fee_percentage': 3.5,
                        'fee_amount': float(fee_amount),
                        'user_receives': float(user_receives),
                        'total_amount_processed': float(amount)  # Full amount merchant will receive
                    })
                )
                withdrawal.save()

                # Lock the full amount from user's balance
                sender_portfolio.balance_usd -= amount
                sender_portfolio.save()

                # Create notification for merchant - LINK TO WITHDRAWAL
                create_and_send_notification(
                    user=merchant,
                    title="Withdrawal Request",
                    message=f"New P2P withdrawal request from {user.username} for ${amount}. "
                           f"User will receive ${user_receives:.2f} (after 5% fee). "
                           f"Bank details: {form_data['bank_name']} - {form_data['account_number']} ({form_data['account_name']})",
                    action_buttons=False,
                    withdrawal=withdrawal
                )

                # Create notification for user - LINK TO WITHDRAWAL
                create_and_send_notification(
                    user=user,
                    title="Withdrawal Submitted",
                    message=f"P2P withdrawal request created for ${amount}. "
                           f"You will receive ${user_receives:.2f} (after 5% fee). "
                           f"Confirm once you receive payment from merchant.",
                    action_buttons=True,
                    withdrawal=withdrawal
                )

                return JsonResponse({
                    'status': 'success', 
                    'message': f'Withdrawal request created. You will receive ${user_receives:.2f} (after 5% fee). Confirm once merchant sends payment.',
                    'withdrawal_id': withdrawal.id,
                    'user_receives': float(user_receives),
                    'fee_amount': float(fee_amount)
                })

            # --- Internal Transfer Logic ---
            elif form_data['method'] == 'INTERNAL':
                recipient_account_number = form_data.get('account_number')
                if not recipient_account_number:
                    return JsonResponse({'status': 'error', 'message': 'Recipient account number required'}, status=400)

                try:
                    recipient_portfolio = UserPortfolio.objects.select_for_update().get(account_number=recipient_account_number)
                except UserPortfolio.DoesNotExist:
                    return JsonResponse({'status': 'error', 'message': 'Recipient account number not found'}, status=404)

                if recipient_portfolio.user == user:
                    return JsonResponse({'status': 'error', 'message': 'Cannot transfer to your own account'}, status=400)

                # Perform the transfer immediately (internal transfers don't need approval)
                sender_portfolio.balance_usd -= amount
                recipient_portfolio.balance_usd += amount
                sender_portfolio.save(update_fields=['balance_usd'])
                recipient_portfolio.save(update_fields=['balance_usd'])

                # Create a COMPLETED withdrawal record for the internal transfer
                withdrawal = Withdrawal(
                    user=user,
                    amount=amount,
                    method='INTERNAL',
                    status='COMPLETED',
                    to_address=recipient_account_number
                )
                withdrawal.save()
                
                return JsonResponse({'status': 'success', 'message': f'Successfully transferred ${amount} to account {recipient_account_number}.'})

            # --- External Withdrawal Logic ---
            else:
                # Create withdrawal with PENDING status
                withdrawal = Withdrawal(
                    user=user,
                    amount=amount,
                    method=form_data['method'],
                    status='PENDING'
                )

                # Handle different withdrawal methods
                if form_data['method'] == 'BYBIT':
                    if not form_data.get('email'):
                        return JsonResponse({'status': 'error', 'message': 'Email required'}, status=400)
                    withdrawal.to_address = form_data['email']

                elif form_data['method'] == 'ON_CHAIN':
                    if not all([form_data.get('wallet_address'), form_data.get('chain')]):
                        return JsonResponse({'status': 'error', 'message': 'Wallet address and chain required'}, status=400)
                    withdrawal.to_address = form_data['wallet_address']
                    withdrawal.chain = form_data['chain']

                withdrawal.save()
                
                # Lock the funds
                sender_portfolio.balance_usd -= amount
                sender_portfolio.save(update_fields=['balance_usd'])
                
                # Create notification for admin - LINK TO WITHDRAWAL
                admin_user = User.objects.filter(is_staff=True).first()
                if admin_user:
                    create_and_send_notification(
                        user=admin_user,
                        title="Withdrawal submitted",
                        message=f"New withdrawal request #{withdrawal.id} for ${withdrawal.amount} from {user.username} requires approval.",
                        withdrawal=withdrawal
                    )
                
                return JsonResponse({
                    'status': 'success', 
                    'message': 'Withdrawal will be processed before 24hours.',
                    'withdrawal_id': withdrawal.id
                })

    except json.JSONDecodeError:
        return JsonResponse({'status': 'error', 'message': 'Invalid JSON'}, status=400)
    except Exception as e:
        return JsonResponse({'status': 'error', 'message': str(e)}, status=500)
    

@api_view(['GET'])
@permission_classes([IsAuthenticated])
def get_referral_code(request):
    """
    Get user's referral code and stats.
    Updated to match new immediate bonus system.
    """
    try:
        user_portfolio = UserPortfolio.objects.get(user=request.user)
        
        # If initial deposit is null, try to set it from first approved deposit
        if user_portfolio.initial_deposit_amount is None:
            first_deposit = Deposit.objects.filter(
                user=request.user,
                status='APPROVED'
            ).order_by('timestamp').first()
            
            if first_deposit:
                user_portfolio.initial_deposit_amount = first_deposit.amount
                user_portfolio.save()
                logger.info(f"Updated initial deposit for user {request.user.username}: {first_deposit.amount}")

        # Get referral stats
        total_referrals = Affiliate.objects.filter(referrer=request.user).count()
        funded_referrals = Affiliate.objects.filter(
            referrer=request.user,
            has_funded_wallet=True
        ).count()

        # Calculate total bonus earned from all referrals
        total_bonus_earned = Decimal('0')
        referrals_with_bonus = Affiliate.objects.filter(
            referrer=request.user,
            has_funded_wallet=True
        ).select_related('referred_user')
        
        for referral in referrals_with_bonus:
            # Get the referred user's first deposit
            first_deposit = Deposit.objects.filter(
                user=referral.referred_user,
                status='APPROVED'
            ).order_by('timestamp').first()
            
            if first_deposit:
                bonus = Decimal(str(first_deposit.amount)) * Decimal('0.15')
                total_bonus_earned += bonus

        return Response({
            "referral_code": user_portfolio.referral_code,
            "stats": {
                "total": total_referrals,
                "funded": funded_referrals,
                "total_bonus_earned": float(total_bonus_earned),
                "has_received_bonus": funded_referrals > 0,  # Any funded referral means bonus received
                "initial_deposit": float(user_portfolio.initial_deposit_amount) if user_portfolio.initial_deposit_amount else None
            }
        })
    except UserPortfolio.DoesNotExist:
        return Response({"error": "User portfolio not found"}, status=404)


@api_view(['POST'])
@authentication_classes([TokenAuthentication])
@permission_classes([IsAuthenticated])
def trade_cryptocurrency(request):
    """
    Handles cryptocurrency trading operations with improved validation and error handling.
    Supports both quantity-based and dollar amount-based trading.
    """
    if request.method != 'POST':
        return JsonResponse({"status": "error", "message": "Invalid request method."}, status=405)

    try:
        # Parse request data - handle both JSON and form data
        if request.content_type == 'application/json':
            data = json.loads(request.body)
        else:
            data = request.data

        trade_type = data.get('trade_type', '').upper()
        symbol = data.get('symbol', '')
        input_type = data.get('input_type', 'quantity')  # 'quantity' or 'amount'

        # Handle both quantity and dollar amount inputs
        if input_type == 'quantity':
            try:
                quantity = Decimal(str(data.get('quantity', '0')))
                dollar_amount = None
            except (InvalidOperation, TypeError):
                return JsonResponse({"status": "error", "message": "Invalid quantity value."}, status=400)
        else:  # input_type == 'amount'
            try:
                dollar_amount = Decimal(str(data.get('amount', '0')))
                quantity = None
            except (InvalidOperation, TypeError):
                return JsonResponse({"status": "error", "message": "Invalid amount value."}, status=400)

        if not all([trade_type, symbol]):
            return JsonResponse({"status": "error", "message": "Trade type and symbol are required."}, status=400)

        if input_type == 'quantity' and (not quantity or quantity <= 0):
            return JsonResponse({"status": "error", "message": "Quantity must be greater than 0."}, status=400)
        
        if input_type == 'amount' and (not dollar_amount or dollar_amount <= 0):
            return JsonResponse({"status": "error", "message": "Amount must be greater than 0."}, status=400)

        if trade_type not in ['BUY', 'SELL']:
            return JsonResponse({"status": "error", "message": "Trade type must be either 'BUY' or 'SELL'."}, status=400)

        user = request.user

        # Check if user account is frozen
        if hasattr(user, 'userportfolio') and user.userportfolio.is_frozen:
            return JsonResponse({
                "status": "error", 
                "message": "Account is suspended. Cannot process transactions."
            }, status=403)

        try:
            crypto = SyntheticAsset.objects.get(symbol=symbol)
        except SyntheticAsset.DoesNotExist:
            return JsonResponse({"status": "error", "message": f"Cryptocurrency with symbol '{symbol}' not found."}, status=404)

        try:
            portfolio = UserPortfolio.objects.get(user=user)
        except UserPortfolio.DoesNotExist:
            return JsonResponse({"status": "error", "message": "User portfolio not found."}, status=404)

        price = Decimal(str(crypto.price_usd))

        # Calculate quantity and amount based on input type
        if input_type == 'quantity':
            amount = quantity
            cost = amount * price
        else:  # input_type == 'amount'
            cost = dollar_amount
            amount = cost / price  # Calculate quantity from dollar amount

        trading_fee = cost * Decimal('0.005')

        with transaction.atomic():
            if trade_type == 'BUY':
                total_cost = cost + trading_fee

                if portfolio.balance_usd < total_cost:
                    return JsonResponse({
                        "status": "error",
                        "message": f"Insufficient balance. Required: ${total_cost:.2f}, Available: ${portfolio.balance_usd:.2f}"
                    }, status=400)

                portfolio.balance_usd -= total_cost
                portfolio.save()

                trade = Trade.objects.create(
                    user=user,
                    asset=crypto,
                    trade_type='BUY',
                    quantity=amount,
                    price_at_trade=price
                )

                # Update user asset balance
                user_asset, created = UserAsset.objects.get_or_create(user=user, asset=crypto)
                user_asset.balance += float(amount)
                user_asset.save()

                return JsonResponse({
                    "status": "success",
                    "message": f"Successfully bought {amount:.6f} {crypto.symbol} for ${cost:.2f}",
                    "trade_id": trade.id,
                    "trade_data": {
                        "trade_type": "BUY",
                        "crypto": crypto.symbol,
                        "amount": float(amount),
                        "dollar_value": float(cost),
                        "price": float(price),
                        "total_cost": float(total_cost),
                        "fee": float(trading_fee),
                        "input_type": input_type
                    }
                })

            elif trade_type == 'SELL':
                user_trades = Trade.objects.filter(user=user, asset=crypto)
                total_bought = sum(t.quantity for t in user_trades.filter(trade_type='BUY'))
                total_sold = sum(t.quantity for t in user_trades.filter(trade_type='SELL'))
                available_amount = total_bought - total_sold

                if available_amount < amount:
                    return JsonResponse({
                        "status": "error",
                        "message": f"Insufficient {crypto.symbol}. Required: {amount:.6f}, Available: {available_amount:.6f}"
                    }, status=400)

                total_revenue = cost - trading_fee
                portfolio.balance_usd += total_revenue
                portfolio.save()

                trade = Trade.objects.create(
                    user=user,
                    asset=crypto,
                    trade_type='SELL',
                    quantity=amount,
                    price_at_trade=price
                )

                # Update user asset balance
                user_asset, created = UserAsset.objects.get_or_create(user=user, asset=crypto)
                user_asset.balance -= float(amount)
                user_asset.save()

                return JsonResponse({
                    "status": "success",
                    "message": f"Successfully sold {amount:.6f} {crypto.symbol} for ${cost:.2f}",
                    "trade_id": trade.id,
                    "trade_data": {
                        "trade_type": "SELL",
                        "crypto": crypto.symbol,
                        "amount": float(amount),
                        "dollar_value": float(cost),
                        "price": float(price),
                        "total_revenue": float(total_revenue),
                        "fee": float(trading_fee),
                        "input_type": input_type
                    }
                })

    except json.JSONDecodeError:
        return JsonResponse({"status": "error", "message": "Invalid JSON data."}, status=400)
    except Exception as e:
        logger.error(f"Error processing trade: {e}", exc_info=True)
        return JsonResponse({"status": "error", "message": "An error occurred during the trade."}, status=500)


@api_view(['POST'])
@authentication_classes([TokenAuthentication])
@permission_classes([IsAuthenticated])
@transaction.atomic
def swap_tokens(request):
    try:
        data = request.data
        user = request.user
        
        # Check if user account is frozen
        if hasattr(user, 'userportfolio') and user.userportfolio.is_frozen:
            return Response({
                "error": "Account is suspended. Cannot process transactions."
            }, status=403)
        
        # Validate required fields
        required_fields = ['from_asset', 'to_asset', 'swap_amount', 'swap_back_asset', 'swap_back_time']
        if not all(field in data for field in required_fields):
            return Response({"error": "Missing required fields."}, status=400)
        
        # Get USDT asset
        try:
            usdt_asset = SyntheticAsset.objects.get(symbol='USDT')
        except SyntheticAsset.DoesNotExist:
            return Response({"error": "USDT asset not found in the system."}, status=400)
        
        # Get assets and validate them
        try:
            from_asset = get_object_or_404(SyntheticAsset, id=data['from_asset'])
            if from_asset.symbol != 'USDT':
                return Response({"error": "Swap from asset must be USDT."}, status=400)
                
            to_asset = get_object_or_404(SyntheticAsset, id=data['to_asset'])
            
            swap_back_asset = get_object_or_404(SyntheticAsset, id=data['swap_back_asset'])
            if swap_back_asset.symbol != 'USDT':
                return Response({"error": "Swap back asset must be USDT."}, status=400)
        except ValueError:
            return Response({"error": "Invalid asset ID format."}, status=400)
        
        # Convert to Decimal
        try:
            swap_amount = Decimal(str(data['swap_amount']))
        except (ValueError, TypeError, InvalidOperation):
            return Response({"error": "Invalid swap amount."}, status=400)
        
        if from_asset == to_asset:
            return Response({"error": "Cannot swap the same asset."}, status=400)
        
        if swap_amount <= 0:
            return Response({"error": "Swap amount must be greater than 0."}, status=400)
        
        # Check user balance
        try:
            portfolio = UserPortfolio.objects.select_for_update().get(user=user)
            if portfolio.balance_usd < swap_amount:
                return Response({
                    "error": f"Insufficient balance. Required: ${swap_amount}, Available: ${portfolio.balance_usd}"
                }, status=400)
        except UserPortfolio.DoesNotExist:
            return Response({"error": "User portfolio not found."}, status=404)
        
        # Process swap time - consistent conversion to Africa/Lagos timezone
        try:
            # Log the received time format for debugging
            logger.info(f"Received swap_back_time: {data['swap_back_time']}")
            
            swap_time = parse_datetime(data['swap_back_time'])
            
            if swap_time is None:
                # Try parsing with alternative formats
                try:
                    # Try direct format
                    swap_time = datetime.datetime.strptime(data['swap_back_time'], '%Y-%m-%dT%H:%M:%S')
                except ValueError:
                    return Response({
                        "error": f"Invalid swap time format. Received: '{data['swap_back_time']}'. Use ISO format (YYYY-MM-DDTHH:MM:SS)."
                    }, status=400)
            
            # Always treat incoming time as local (server) time if no timezone is specified
            if swap_time.tzinfo is None:
                # First make it timezone aware in server time
                server_tz = timezone.get_current_timezone()
                swap_time = timezone.make_aware(swap_time, server_tz)
            
            # Convert to Africa/Lagos timezone
            swap_time = swap_time.astimezone(lagos_tz)
            
            logger.info(f"Processed swap_time in Lagos timezone: {swap_time}")
            
            # Ensure swap time is in the future
            current_time = timezone.now().astimezone(lagos_tz)
            if swap_time <= current_time:
                return Response({"error": "Swap time must be in the future."}, status=400)
                
            # Validate duration (5 minutes to 30 days)
            duration_minutes = (swap_time - current_time).total_seconds() / 60
            if duration_minutes < 5:
                return Response({"error": "Swap duration must be at least 5 minutes."}, status=400)
            if duration_minutes > 43200:  # 30 days
                return Response({"error": "Swap duration cannot exceed 30 days."}, status=400)
                
        except (ValueError, AttributeError) as e:
            # Enhanced error reporting with exception details
            logger.error(f"Date parsing error: {str(e)}")
            return Response({
                "error": f"Invalid swap time format: {str(e)}. Expected ISO format (YYYY-MM-DDTHH:MM:SS)."
            }, status=400)
        
        # Get current price of to_asset
        to_asset_price = Decimal(str(to_asset.price_usd))

        # Create the swap request (with proper timezone storage)
        swap_request = SwapRequest.objects.create(
            user=user,
            from_asset=from_asset,
            to_asset=to_asset,
            swap_back_asset=swap_back_asset,
            swap_amount=swap_amount,
            swap_time=swap_time,
            original_to_asset_price=to_asset_price,
            status='PENDING'
        )
        
        # Lock the amount
        portfolio.balance_usd -= swap_amount
        portfolio.save(update_fields=['balance_usd'])
        
        # Create notification for user
        create_and_send_notification(
            user=user,
            title="Swap submitted",
            message=f"Your swap request for {swap_amount} USDT to {to_asset.symbol} has been created and is now pending. "
                f"Scheduled for {swap_time.strftime('%Y-%m-%d %H:%M:%S %Z')}",
            swap=swap_request,
            action_buttons=True  # Show action buttons for swap notifications
        )
        # Create notification for admin 
        try:
            from django.contrib.auth.models import User
            admin_user = User.objects.filter(is_staff=True).first()
            if admin_user:
                create_and_send_notification(
                    user=admin_user,
                    title="Swap submitted",
                    message=f"New swap request for {swap_amount} USDT to {to_asset.symbol}, "
                        f"scheduled for {swap_time.strftime('%Y-%m-%d %H:%M:%S %Z')}",
                    swap=swap_request,
                    action_buttons=True  # Show action buttons for admin notifications
                )
        except Exception as e:
            # Don't fail the whole process if notification creation fails
            logger.error(f"Error creating admin notification: {str(e)}")
        
        return Response({
            "message": "Swap request created successfully.",
            "swap_id": swap_request.id,
            "swap_time": swap_time.strftime('%Y-%m-%d %H:%M:%S %Z'),
            "status": "PENDING"
        }, status=201)
        
    except Exception as e:
        logger.error(f"Error creating swap request: {str(e)}", exc_info=True)
        return Response({"error": str(e)}, status=500)

@api_view(['GET'])
@authentication_classes([TokenAuthentication])
@permission_classes([IsAuthenticated])
def get_user_portfolio(request):
    try:
        portfolio = UserPortfolio.objects.get(user=request.user)
        user_assets = UserAsset.objects.filter(user=request.user, balance__gt=0).select_related('asset')
        tokens = [
            {
                "symbol": ua.asset.symbol,
                "name": ua.asset.name,
                "image_url": ua.asset.image_url,
                "balance": ua.balance
            }
            for ua in user_assets
        ]

        data = {
            "user": {
                "username": request.user.username,
                "email": request.user.email
            },
            "account_number": portfolio.account_number,
            "balance_usd": float(portfolio.balance_usd),
            "referral_code": portfolio.referral_code,
            "tokens": tokens
        }
        return Response(data)
    except UserPortfolio.DoesNotExist:
        logger.error(f"Portfolio not found for user {request.user.id}")
        return Response({"error": "User portfolio not found."}, status=404)
    except Exception as e:
        logger.error(f"Error fetching user portfolio: {str(e)}", exc_info=True)
        return Response({"error": "Failed to retrieve user portfolio."}, status=500)
    

@api_view(['GET'])
@authentication_classes([TokenAuthentication])
@permission_classes([IsAuthenticated])
def get_crypto_prices(request):
    """Retrieves cryptocurrency prices with caching and logging, ordered by admin-controlled display_order."""
    try:
        # Order by display_order first (lower numbers first), then by ID as fallback
        cryptos = SyntheticAsset.objects.all().order_by('display_order', 'id')
        serializer = SyntheticAssetSerializer(cryptos, many=True)
                
        response_data = {
            "cryptocurrencies": serializer.data,
        }
                
        return Response(response_data)
        
    except Exception as e:
        logger.error(f"Error fetching cryptocurrency prices: {str(e)}", exc_info=True)
        return Response({"error": "Failed to retrieve cryptocurrency prices."}, status=500)


@api_view(['GET'])
@authentication_classes([TokenAuthentication])
@permission_classes([IsAuthenticated])
def check_pending_swap(request):
    """
    Checks if the user has any pending swap requests.
    """
    has_pending_swap = SwapRequest.objects.filter(user=request.user, status='PENDING').exists()
    return Response({"has_pending_swap": has_pending_swap})            




@api_view(['POST'])
@authentication_classes([TokenAuthentication])
@permission_classes([IsAuthenticated])
def change_username(request):
    """Change the logged-in user's username after password verification."""
    try:
        serializer = ChangeUsernameSerializer(data=request.data, context={'request': request})
        if serializer.is_valid():
            serializer.save()
            logger.info(f"User {request.user.id} changed username to {request.data.get('new_username')}")
            return Response({
                "status": "success",
                "message": "Username successfully updated."
            })
        else:
            logger.warning(f"Invalid username change attempt by user {request.user.id}: {serializer.errors}")
            return Response({
                "status": "error",
                "message": serializer.errors
            }, status=status.HTTP_400_BAD_REQUEST)
    except Exception as e:
        logger.error(f"Error changing username for user {request.user.id}: {str(e)}", exc_info=True)
        return Response({
            "status": "error",
            "message": "An unexpected error occurred."
        }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)


@api_view(['POST'])
@authentication_classes([TokenAuthentication])
@permission_classes([IsAuthenticated])
@ratelimit(key='user', rate='3/h', block=True)  # Limit password change attempts
def change_password(request):
    """Change the logged-in user's password with verification."""
    try:
        serializer = ChangePasswordSerializer(data=request.data, context={'request': request})
        if serializer.is_valid():
            serializer.save()
            logger.info(f"User {request.user.id} successfully changed password")
            
            # Force user to login again with new password (optional)
            # logout(request)
            
            return Response({
                "status": "success",
                "message": "Password successfully updated."
            })
        else:
            logger.warning(f"Invalid password change attempt by user {request.user.id}: {serializer.errors}")
            return Response({
                "status": "error",
                "message": serializer.errors
            }, status=status.HTTP_400_BAD_REQUEST)
    except Exception as e:
        logger.error(f"Error changing password for user {request.user.id}: {str(e)}", exc_info=True)
        return Response({
            "status": "error",
            "message": "An unexpected error occurred."
        }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)
    


@api_view(['GET'])
@authentication_classes([TokenAuthentication])
@permission_classes([IsAuthenticated])
def get_user_transactions(request):
    try:
        withdrawals = Withdrawal.objects.filter(user=request.user).order_by('-timestamp')
        formatted_withdrawals = []
        
        for withdrawal in withdrawals:
            formatted_withdrawal = {
                'id': withdrawal.id,
                'amount': withdrawal.amount,
                'status': withdrawal.status,
                'method': withdrawal.method,
                'timestamp': withdrawal.timestamp,
                'created_at': withdrawal.timestamp,  # Ensure created_at is always provided for consistent dating
                'to_address': withdrawal.to_address,
                'chain': withdrawal.chain
            }
            
            # Format display method and recipient details
            if withdrawal.method == 'INTERNAL':
                formatted_withdrawal['display_method'] = 'Internal Transfer'
                formatted_withdrawal['recipient_details'] = f"Account: {withdrawal.to_address}"  # Account number for internal transfers
            elif withdrawal.method == 'BANK':
                try:
                    bank_details = json.loads(withdrawal.to_address)
                    formatted_withdrawal['display_method'] = 'Bank Transfer'
                    formatted_withdrawal['recipient_details'] = f"{bank_details['bank_name']} - {bank_details['account_number']}"
                except:
                    formatted_withdrawal['display_method'] = 'Bank Transfer'
                    formatted_withdrawal['recipient_details'] = withdrawal.to_address
            elif withdrawal.method == 'BYBIT':
                formatted_withdrawal['display_method'] = 'Bybit'
                formatted_withdrawal['recipient_details'] = f"UID: {withdrawal.to_address}"
            elif withdrawal.method == 'ON_CHAIN':
                formatted_withdrawal['display_method'] = 'On-Chain'
                formatted_withdrawal['recipient_details'] = f"{withdrawal.to_address} {f'({withdrawal.chain})' if withdrawal.chain else ''}"
            else:
                formatted_withdrawal['display_method'] = withdrawal.method
                formatted_withdrawal['recipient_details'] = withdrawal.to_address
            
            formatted_withdrawals.append(formatted_withdrawal)
    
        # Format trades and deposits too, ensuring created_at is always provided
        trades = list(Trade.objects.filter(user=request.user).order_by('-timestamp').values(
            'id', 'trade_type', 'quantity', 'price_at_trade', 'asset__symbol', 'timestamp'
        ))
        for trade in trades:
            trade['created_at'] = trade['timestamp']  # Ensure created_at for consistent dating
        
        deposits = list(Deposit.objects.filter(user=request.user).order_by('-timestamp').values(
            'id', 'amount', 'status', 'method', 'timestamp'
        ))
        for deposit in deposits:
            deposit['created_at'] = deposit['timestamp']  # Ensure created_at for consistent dating

        return JsonResponse({
            "trades": trades,
            "deposits": deposits,
            "withdrawals": formatted_withdrawals
        }, safe=False)
    except Exception as e:
        logger.error(f"Error fetching transactions: {str(e)}", exc_info=True)
        return JsonResponse({"error": "Failed to retrieve transactions."}, status=500)



@api_view(['POST'])
@authentication_classes([TokenAuthentication])
@permission_classes([IsAuthenticated])
def create_synthetic_asset(request):
    try:
        # Check user's star rating
        portfolio = UserPortfolio.objects.get(user=request.user)
        star_rating = 1  # Default rating
        
        if portfolio.balance_usd >= 5000:
            star_rating = 5
        elif portfolio.balance_usd >= 1001:
            star_rating = 4
        elif portfolio.balance_usd >= 501:
            star_rating = 3
        elif portfolio.balance_usd >= 101:
            star_rating = 2

        # Changed from 5 to 3 stars
        if star_rating < 4:
            return Response({
                "status": "error",
                "message": "You need to be at least a 4-star user to create synthetic assets."
            }, status=403)

        # Validate input
        name = request.data.get('name')
        symbol = request.data.get('symbol', '').upper()
        image_url = request.data.get('image_url')

        if not all([name, symbol, image_url]):
            return Response({
                "status": "error",
                "message": "Name, symbol and image URL are required."
            }, status=400)

        # Check if symbol already exists
        if SyntheticAsset.objects.filter(symbol=symbol).exists():
            return Response({
                "status": "error",
                "message": "This symbol is already in use."
            }, status=400)

        # Create the asset
        asset = SyntheticAsset.objects.create(
            name=name,
            symbol=symbol,
            image_url=image_url,
            created_by=request.user,
            price_usd=0.00001,  # Initial price
            total_supply=1000000.0  # Initial supply
        )

        # Notify admins
        admin_users = User.objects.filter(is_staff=True)
        for admin in admin_users:
            Notification.objects.create(
                user=admin,
                message=f"New synthetic asset {symbol} created by {request.user.username} requires verification."
            )

        return Response({
            "status": "success",
            "message": "Asset created successfully and is pending admin verification.",
            "asset": {
                "id": asset.id,
                "name": asset.name,
                "symbol": asset.symbol,
                "price_usd": asset.price_usd
            }
        })

    except Exception as e:
        logger.error(f"Error creating synthetic asset: {str(e)}", exc_info=True)
        return Response({
            "status": "error",
            "message": "Failed to create synthetic asset."
        }, status=500)


@api_view(['POST'])
@authentication_classes([TokenAuthentication])
@permission_classes([IsAuthenticated])
def apply_merchant(request):
    try:
        # Log incoming request data for debugging
        print(f"Request data: {request.data}")
        print(f"Request method: {request.method}")
        print(f"Content-Type: {request.content_type}")
        print(f"User: {request.user.username}")
        
        # Validate required fields are present
        required_fields = ['name', 'bank_name', 'account_number']
        for field in required_fields:
            if field not in request.data or not request.data.get(field):
                return Response({
                    "error": f"Missing required field: {field}",
                    "required_fields": required_fields
                }, status=400)
        
        # Check if user is at least 4-star
        try:
            portfolio = UserPortfolio.objects.get(user=request.user)
            print(f"User balance: ${portfolio.balance_usd}")
        except UserPortfolio.DoesNotExist:
            return Response({"error": "User portfolio not found"}, status=404)
            
        if portfolio.balance_usd < 1001:  
            return Response({"error": "You need to be at least a 4-star user to become a merchant"}, status=403)
            
        # Check if already applied
        if MerchantApplication.objects.filter(user=request.user).exists():
            return Response({"error": "You already have a pending application"}, status=400)
            
        serializer = MerchantApplicationSerializer(data=request.data)
        if serializer.is_valid():
            application = serializer.save(user=request.user, status='PENDING')
            
            # Notify admin
            admin_users = User.objects.filter(is_staff=True)
            for admin in admin_users:
                Notification.objects.create(
                    user=admin,
                    message=f"New merchant application from {request.user.username}"
                )
                
            return Response({
                "message": "Application submitted successfully",
                "application_id": application.id
            }, status=201)
        else:
            # Log serializer errors for debugging
            print(f"Serializer validation failed!")
            print(f"Serializer errors: {serializer.errors}")
            print(f"Serializer non-field errors: {serializer.non_field_errors()}")
            
            # Check individual field validation
            for field_name, field_value in request.data.items():
                print(f"Field '{field_name}': '{field_value}' (type: {type(field_value)}, length: {len(str(field_value)) if field_value else 0})")
            
            return Response({
                "error": "Invalid data provided",
                "details": serializer.errors,
                "field_info": {field: str(value) for field, value in request.data.items()}
            }, status=400)
            
    except Exception as e:
        print(f"Unexpected error in apply_merchant: {str(e)}")
        return Response({"error": "Internal server error"}, status=500)

        
@api_view(['POST'])
@authentication_classes([TokenAuthentication])
@permission_classes([IsAuthenticated])
def confirm_merchant_payment(request, deposit_id):
    try:
        deposit = Deposit.objects.get(id=deposit_id, method='BANK_TRANSFER')
        if deposit.merchant != request.user:
            return Response({"error": "Not authorized"}, status=403)
            
        if deposit.status != 'PENDING':
            return Response({"error": "Payment already processed"}, status=400)
            
        # Update deposit status
        deposit.status = 'APPROVED'
        deposit.save()
        
        # Update merchant balance
        merchant_portfolio = UserPortfolio.objects.get(user=deposit.merchant)
        merchant_portfolio.balance_usd -= Decimal(str(deposit.amount))
        merchant_portfolio.save()
        
        # Notify depositor
        Notification.objects.create(
            user=deposit.user,
            message=f"Your deposit of ${deposit.amount} has been confirmed by merchant"
        )
        
        return Response({"message": "Payment confirmed successfully"})
    except Exception as e:
        return Response({"error": str(e)}, status=500)


@api_view(['GET'])
@permission_classes([IsAuthenticated])
def get_approved_merchants(request):
    try:
        # Get merchants from MerchantApplication model
        approved_merchants = MerchantApplication.objects.filter(
            status='APPROVED'
        ).select_related('user')
        
        merchants_data = []
        for merchant in approved_merchants:
            merchants_data.append({
                'id': merchant.id,
                'name': merchant.name,
                'bank_name': merchant.bank_name,
                'account_number': merchant.account_number,
                'star_rating': 5,  # All approved merchants are 5-star
                'verified': True
            })
        
        return Response(merchants_data)
    except Exception as e:
        logger.error(f"Error fetching merchants: {str(e)}")
        return Response(
            {"error": "Failed to load merchants"}, 
            status=status.HTTP_500_INTERNAL_SERVER_ERROR
        )


@api_view(['GET'])
@authentication_classes([TokenAuthentication])
@permission_classes([IsAuthenticated])
def get_notifications(request):
    """Get user's notifications sorted by timestamp"""
    try:
        # Check if user is a merchant
        is_merchant = False
        try:
            portfolio = UserPortfolio.objects.get(user=request.user)
            is_merchant = portfolio.is_merchant
        except UserPortfolio.DoesNotExist:
            pass
            
        notifications = Notification.objects.filter(
            user=request.user
        ).order_by('-timestamp')
        
        notification_data = []
        for notification in notifications:
            # Generate dynamic title based on notification type
            title = "Notification"  # Default title
            if notification.deposit:
                title = "Deposit Update"
            elif notification.withdrawal:
                title = "Withdrawal Update"
            elif notification.swap:
                title = "Swap Update"
            
            # Determine if action buttons should be shown
            show_action_buttons = False
            
            if notification.action_buttons:
                # For deposit notifications - show to merchants only
                if notification.deposit:
                    show_action_buttons = is_merchant
                
                # For withdrawal notifications - show to the user who made the withdrawal
                # (specifically for P2P withdrawals where user needs to confirm receipt)
                elif notification.withdrawal:
                    # Check if this is a P2P withdrawal confirmation for the user
                    withdrawal = notification.withdrawal
                    if (withdrawal.method == 'BANK' and 
                        withdrawal.user == request.user and 
                        withdrawal.user_confirmation_required and
                        withdrawal.status == 'PENDING'):
                        show_action_buttons = True
                
                # For other notification types, keep existing logic
                else:
                    show_action_buttons = is_merchant
            
            notification_data.append({
                'id': notification.id,
                'title': title,  # Use dynamically generated title
                'message': notification.message,
                'is_read': notification.is_read,
                'timestamp': notification.timestamp,
                'action_buttons': show_action_buttons,
                'deposit_id': notification.deposit.id if notification.deposit else None,
                'withdrawal_id': notification.withdrawal.id if notification.withdrawal else None,
                'swap_id': notification.swap.id if notification.swap else None,
            })
        
        return Response(notification_data)
        
    except Exception as e:
        logger.error(f"Error fetching notifications: {str(e)}")
        return Response(
            {"error": "Failed to fetch notifications"}, 
            status=status.HTTP_500_INTERNAL_SERVER_ERROR
        )

@api_view(['PATCH'])
@authentication_classes([TokenAuthentication])
@permission_classes([IsAuthenticated])
def update_notification(request, notification_id):
    """Mark a notification as read"""
    try:
        notification = Notification.objects.get(
            id=notification_id,
            user=request.user
        )
        notification.is_read = True
        notification.save()
        return Response({
            'message': 'Notification marked as read',
            'notification_id': notification_id
        })
    except Notification.DoesNotExist:
        return Response(
            {"error": "Notification not found"}, 
            status=status.HTTP_404_NOT_FOUND
        )
    except Exception as e:
        logger.error(f"Error updating notification: {str(e)}")
        return Response(
            {"error": "Failed to update notification"}, 
            status=status.HTTP_500_INTERNAL_SERVER_ERROR
        )


# views.py - Updated merchant_approve_deposit function
@api_view(['POST'])
@authentication_classes([TokenAuthentication])
@permission_classes([IsAuthenticated])
def merchant_approve_deposit(request, deposit_id):
    """When merchant approves - their balance gets deducted for base amount only"""
    try:
        with transaction.atomic():
            # Get the deposit that needs merchant approval
            deposit = Deposit.objects.select_for_update().get(
                id=deposit_id,
                merchant=request.user,  # Only the assigned merchant can approve
                status='PENDING',
                merchant_action_required=True
            )

            # Parse fee information from deposit notes
            fee_info = json.loads(deposit.deposit_notes) if deposit.deposit_notes else {}
            base_amount = Decimal(str(fee_info.get('base_amount', deposit.amount)))
            fee_amount = Decimal(str(fee_info.get('fee_amount', 0)))
            total_amount_with_fee = base_amount + fee_amount

            # Get merchant's portfolio
            merchant_portfolio = UserPortfolio.objects.select_for_update().get(
                user=request.user
            )

            # Check if merchant has enough balance for base amount only (not including fee)
            if merchant_portfolio.balance_usd < base_amount:
                return Response({
                    "status": "error",
                    "message": f"Insufficient balance. You need ${base_amount} but only have ${merchant_portfolio.balance_usd}"
                }, status=400)

            # DEDUCT only base amount from merchant's balance (merchant keeps the fee)
            merchant_portfolio.balance_usd -= base_amount
            merchant_portfolio.save()

            # Update deposit status
            deposit.status = 'APPROVED'
            deposit.merchant_action_required = False
            deposit.save()

            # CREDIT user's balance with base amount (user receives base amount)
            user_portfolio = UserPortfolio.objects.select_for_update().get(
                user=deposit.user
            )
            user_portfolio.balance_usd += base_amount
            user_portfolio.save()

            # Create notifications
            create_and_send_notification(
                user=deposit.user,  # Notify the USER
                title="Deposit submitted",
                message=f"Approved! ${base_amount} has been added to your account by {request.user.username}",
                
            )

            create_and_send_notification(
                user=request.user,  # Notify the MERCHANT
                title="Deposit submitted",
                message=f"You approved {deposit.user.username}'s deposit. ${base_amount} deducted (you keep ${fee_amount} fee). Your balance: ${merchant_portfolio.balance_usd}",
               
            )

            logger.info(f"Deposit approved: {deposit.user.username} received ${base_amount}, merchant {request.user.username} paid ${base_amount} and kept ${fee_amount} fee, merchant balance: ${merchant_portfolio.balance_usd}")

            return Response({
                "status": "success",
                "message": f"Deposit approved! ${base_amount} sent to {deposit.user.username}. You kept ${fee_amount} fee. Your new balance: ${merchant_portfolio.balance_usd}"
            })

    except Deposit.DoesNotExist:
        return Response({
            "status": "error",
            "message": "Deposit not found or you don't have permission to approve it"
        }, status=404)
    except UserPortfolio.DoesNotExist:
        return Response({
            "status": "error", 
            "message": "Portfolio not found"
        }, status=404)
    except Exception as e:
        logger.error(f"Error approving deposit: {str(e)}", exc_info=True)
        return Response({
            "status": "error",
            "message": "An error occurred while approving deposit"
        }, status=500)

# Updated merchant decline function  
@api_view(['POST'])
@authentication_classes([TokenAuthentication])
@permission_classes([IsAuthenticated])
def merchant_decline_deposit(request, deposit_id):
    """When merchant declines the deposit"""
    try:
        deposit = Deposit.objects.get(
            id=deposit_id, 
            merchant=request.user, 
            status='PENDING',
            merchant_action_required=True
        )
        
        # Update deposit status
        deposit.status = 'REJECTED'
        deposit.merchant_action_required = False
        deposit.save()
        
        # Create notifications
        create_and_send_notification(
            user=deposit.user,  # Notify the USER
            title="Deposit submitted",
            message=f"Not available.",
            
        )
        
        create_and_send_notification(
            user=request.user,  # Notify the MERCHANT
            title="Deposit submitted",
            message=f"You declined {deposit.user.username}'s deposit of ${deposit.amount}",
           
        )
        
        return Response({
            "status": "success",
            "message": "Deposit declined successfully."
        })
        
    except Deposit.DoesNotExist:
        return Response({
            "status": "error",
            "message": "Deposit not found or you don't have permission to decline it."
        }, status=404)
    except Exception as e:
        logger.error(f"Error declining deposit: {str(e)}", exc_info=True)
        return Response({
            "status": "error",
            "message": "An error occurred while declining deposit."
        }, status=500)


@api_view(['POST'])
@authentication_classes([TokenAuthentication])
@permission_classes([IsAuthenticated])
def user_confirm_withdrawal(request, withdrawal_id):
    """Endpoint for user to confirm they received P2P withdrawal"""
    try:
        with transaction.atomic():
            withdrawal = Withdrawal.objects.select_for_update().get(
                id=withdrawal_id,
                user=request.user,
                status='PENDING',
                user_confirmation_required=True
            )

            # Parse fee information
            fee_info = json.loads(withdrawal.withdrawal_notes) if withdrawal.withdrawal_notes else {}
            total_amount = Decimal(str(fee_info.get('total_amount_processed', withdrawal.amount)))

            # Credit merchant's balance with full amount (including fee)
            merchant_portfolio = UserPortfolio.objects.select_for_update().get(
                user=withdrawal.merchant
            )
            merchant_portfolio.balance_usd += total_amount
            merchant_portfolio.save()

            # Update withdrawal status
            withdrawal.status = 'COMPLETED'
            withdrawal.user_confirmation_required = False
            withdrawal.confirmed_at = timezone.now()
            withdrawal.save()

            # Create notifications
            create_and_send_notification(
                user=withdrawal.merchant,
                title="Withdrawal Confirmed",
                message=f"{request.user.username} confirmed receiving payment. ${total_amount:.2f} has been credited to your balance (includes 5% fee).",
                withdrawal=withdrawal
            )

            create_and_send_notification(
                user=request.user,
                title="Withdrawal Confirmed",
                message=f"Withdrawal confirmed successfully. Thank you for confirming receipt.",
                withdrawal=withdrawal
            )

            return Response({
                "status": "success",
                "message": "Withdrawal confirmed successfully. Merchant has been credited."
            })

    except Withdrawal.DoesNotExist:
        return Response({
            "status": "error",
            "message": "Withdrawal not found or already confirmed"
        }, status=404)
    except Exception as e:
        logger.error(f"Error confirming withdrawal: {str(e)}", exc_info=True)
        return Response({
            "status": "error",
            "message": "An error occurred while confirming withdrawal"
        }, status=500)


@api_view(['POST'])
@authentication_classes([TokenAuthentication])
@permission_classes([IsAuthenticated])
def user_decline_withdrawal(request, withdrawal_id):
    """Endpoint for user to decline P2P withdrawal (if payment wasn't received)"""
    try:
        with transaction.atomic():
            withdrawal = Withdrawal.objects.select_for_update().get(
                id=withdrawal_id,
                user=request.user,
                status='PENDING',
                user_confirmation_required=True
            )

            # Refund user's balance
            user_portfolio = UserPortfolio.objects.select_for_update().get(
                user=request.user
            )
            user_portfolio.balance_usd += Decimal(str(withdrawal.amount))
            user_portfolio.save()

            # Update withdrawal status
            withdrawal.status = 'REJECTED'
            withdrawal.user_confirmation_required = False
            withdrawal.save()

            # Create notifications
            create_and_send_notification(
                user=withdrawal.merchant,
                title="Withdrawal submitted",
                message=f"{request.user.username} declined P2P withdrawal",
                
            )
            

            create_and_send_notification(
                user=request.user,
                title="Withdrawal submitted",
                message=f"Withdrawal declined. Your funds have been returned.",
               
            )

            return Response({
                "status": "success",
                "message": "Withdrawal declined. Your funds have been returned."
            })

    except Withdrawal.DoesNotExist:
        return Response({
            "status": "error",
            "message": "Withdrawal not found or already processed"
        }, status=404)
    except Exception as e:
        logger.error(f"Error declining withdrawal: {str(e)}", exc_info=True)
        return Response({
            "status": "error",
            "message": "An error occurred while declining withdrawal"
        }, status=500)     


@api_view(['GET'])
@authentication_classes([TokenAuthentication])
@permission_classes([IsAuthenticated])
def unread_notification_count(request):
    count = Notification.objects.filter(
        user=request.user,
        is_read=False
    ).count()
    return Response({'count': count})        



@api_view(['POST'])
@authentication_classes([TokenAuthentication])
@permission_classes([IsAuthenticated])
def register_fcm_token(request):
    """Register or update FCM token for the user with improved error handling"""
    try:
        token = request.data.get('token')
        device_type = request.data.get('device_type', 'web')
        
        if not token:
            return Response(
                {"error": "FCM token is required"}, 
                status=status.HTTP_400_BAD_REQUEST
            )
        
        # Validate device_type
        valid_device_types = ['web', 'android', 'ios']
        if device_type not in valid_device_types:
            return Response(
                {"error": f"Invalid device_type. Must be one of: {valid_device_types}"}, 
                status=status.HTTP_400_BAD_REQUEST
            )
        
        # Use user and token combination for uniqueness
        fcm_token, created = FCMToken.objects.update_or_create(
            user=request.user,
            token=token,
            defaults={
                'device_type': device_type,
                'is_active': True
            }
        )
        
        # Test the token by sending a welcome notification
        try:
            send_push_notification(
                user=request.user,
                title="Welcome!",
                message="Your device is now registered for notifications.",
                notification_type="test",
                data={'test': 'true'}
            )
        except Exception as e:
            logger.warning(f"Failed to send test notification: {str(e)}")
        
        return Response({
            'message': 'FCM token registered successfully',
            'created': created,
            'device_type': device_type,
            'token_id': fcm_token.id
        }, status=status.HTTP_201_CREATED if created else status.HTTP_200_OK)
    
    except Exception as e:
        logger.error(f"Error registering FCM token for user {request.user.username}: {str(e)}")
        return Response(
            {"error": "Failed to register FCM token"}, 
            status=status.HTTP_500_INTERNAL_SERVER_ERROR
        )


@api_view(['POST'])
@authentication_classes([TokenAuthentication])
@permission_classes([IsAuthenticated])
def unregister_fcm_token(request):
    """Unregister FCM token for the user"""
    try:
        token = request.data.get('token')

        if not token:
            return Response(
                {"error": "FCM token is required"}, 
                status=status.HTTP_400_BAD_REQUEST
            )

        # Try to find and deactivate the token
        try:
            fcm_token = FCMToken.objects.get(user=request.user, token=token)
            fcm_token.is_active = False
            fcm_token.save()
            
            logger.info(f"FCM token deactivated for user {request.user.username}")
            
            return Response({
                'message': 'FCM token unregistered successfully',
                'token_id': fcm_token.id
            })
            
        except FCMToken.DoesNotExist:
            return Response(
                {"error": "FCM token not found for current user"}, 
                status=status.HTTP_404_NOT_FOUND
            )

    except Exception as e:
        logger.error(f"Error unregistering FCM token for user {request.user.username}: {str(e)}")
        return Response(
            {"error": "Failed to unregister FCM token"}, 
            status=status.HTTP_500_INTERNAL_SERVER_ERROR
        )


def suspended_account(request):
    return render(request, 'suspended_account.html')


@api_view(['GET'])
@authentication_classes([TokenAuthentication])
@permission_classes([IsAuthenticated])
def merchant_balances(request):
    """Get current balances of all approved merchants"""
    try:
        approved_merchants = MerchantApplication.objects.filter(
            status='APPROVED'
        ).select_related('user')
        
        merchant_balances = []
        for merchant in approved_merchants:
            try:
                portfolio = UserPortfolio.objects.get(user=merchant.user)
                merchant_balances.append({
                    'id': merchant.id,
                    'balance': float(portfolio.balance_usd)
                })
            except UserPortfolio.DoesNotExist:
                merchant_balances.append({
                    'id': merchant.id,
                    'balance': 0
                })
        
        return Response(merchant_balances)
    except Exception as e:
        logger.error(f"Error fetching merchant balances: {str(e)}")
        return Response(
            {"error": "Failed to load merchant balances"}, 
            status=status.HTTP_500_INTERNAL_SERVER_ERROR
        )