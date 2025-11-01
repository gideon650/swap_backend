# Enhanced CandlestickService - Handles admin price changes with realistic candles

from datetime import datetime, timedelta
from decimal import Decimal
import random
import math
import logging
from django.utils import timezone
from django.core.cache import cache
from .models import SyntheticAsset, CandlestickData

logger = logging.getLogger(__name__)

class CandlestickService:
    
    @staticmethod
    def get_chart_data(asset, interval='15min', candle_count=None):
        """
        Main method to get chart data for any interval
        Now handles admin price changes with immediate candle reflection
        NO CACHING - ALWAYS FRESH DATA
        """
        try:
            logger.info(f"Getting chart data for {asset.symbol} - {interval}")
            
            # Check for recent admin price changes and handle them
            CandlestickService._handle_admin_price_changes(asset)
            
            # Ensure we have base 1-minute data
            CandlestickService._ensure_base_data(asset)
            
            # Get interval in minutes
            interval_minutes = CandlestickService._get_interval_minutes(interval)
            
            # Set default candle count based on interval
            if candle_count is None:
                candle_count = CandlestickService._get_default_candle_count(interval)
            
            # Get aggregated data
            chart_data = CandlestickService._get_aggregated_data(
                asset, interval_minutes, candle_count
            )
            
            logger.info(f"Aggregated data returned {len(chart_data) if chart_data else 0} candles")
            
            if not chart_data:
                logger.warning(f"No aggregated data available for {asset.symbol}")
                return []
            
            # Update with latest price but with reduced variation
            chart_data = CandlestickService._update_latest_candle_realistic(
                asset, chart_data, interval_minutes
            )
            
            # FORCE the last candle to reflect current asset price
            if chart_data and hasattr(asset, 'price_usd') and asset.price_usd:
                current_price = float(asset.price_usd)
                if current_price > 0:  # Only update if price is valid
                    last_candle = chart_data[-1]
                    # Ensure the close price matches current asset price
                    last_candle['close'] = current_price
                    last_candle['high'] = max(last_candle['high'], current_price)
                    last_candle['low'] = min(last_candle['low'], current_price)
                    logger.info(f"Forced last candle close to current asset price: {current_price}")
            
            return chart_data
            
        except Exception as e:
            logger.error(f"Error getting chart data for {asset.symbol}: {e}", exc_info=True)
            return []
    
    @staticmethod
    def _handle_admin_price_changes(asset):
        """
        Detect and handle admin price changes by creating immediate candles
        """
        try:
            # Get the most recent candle
            latest_candle = CandlestickData.get_latest_candle(asset, '1min')
            
            if not latest_candle:
                return
                
            current_price = float(asset.price_usd) if asset.price_usd else 0.0
            if current_price <= 0:
                logger.warning(f"Invalid current price for {asset.symbol}: {current_price}")
                return
                
            last_candle_close = float(latest_candle.close_price)
            
            # Check if there's been a significant price change (>3% threshold for admin changes)
            if last_candle_close > 0:
                price_change_ratio = abs(current_price - last_candle_close) / last_candle_close
                
                if price_change_ratio > 0.03:  # 3% threshold
                    logger.info(f"Detected significant price change for {asset.symbol}: {price_change_ratio:.2%}")
                    
                    # Check if this change happened very recently (within last 3 minutes)
                    time_since_last = timezone.now() - latest_candle.timestamp
                    if time_since_last <= timedelta(minutes=3):
                        # This looks like an admin price change - create immediate adjustment candle
                        CandlestickService._create_admin_price_change_candle(
                            asset, latest_candle, current_price
                        )
                        
        except Exception as e:
            logger.error(f"Error handling admin price changes for {asset.symbol}: {e}")
    
    @staticmethod
    def _create_admin_price_change_candle(asset, last_candle, new_price):
        """
        Create a candle that shows the immediate price change from admin action
        """
        try:
            # Create candle for the current minute
            current_time = timezone.now().replace(second=0, microsecond=0)
            
            # Check if we already have a candle for this minute
            existing_candle = CandlestickData.objects.filter(
                asset=asset,
                timestamp=current_time,
                interval='1min'
            ).first()
            
            if existing_candle:
                # Update existing candle to reflect the price change
                old_close = float(existing_candle.close_price)
                
                # Create a dramatic candle showing the price movement
                if new_price > old_close:
                    # Price went up - create bullish candle
                    existing_candle.high_price = max(float(existing_candle.high_price), new_price)
                    existing_candle.close_price = Decimal(str(new_price))
                else:
                    # Price went down - create bearish candle  
                    existing_candle.low_price = min(float(existing_candle.low_price), new_price)
                    existing_candle.close_price = Decimal(str(new_price))
                    
                existing_candle.save()
                logger.info(f"Updated existing candle for {asset.symbol} with new price {new_price}")
                
            else:
                # Create new candle showing the price jump
                last_close = float(last_candle.close_price)
                
                # Determine OHLC based on direction of change
                if new_price > last_close:
                    # Bullish move
                    open_price = last_close
                    close_price = new_price
                    high_price = new_price * 1.005  # Small wick
                    low_price = last_close * 0.995  # Small wick
                else:
                    # Bearish move
                    open_price = last_close
                    close_price = new_price  
                    high_price = last_close * 1.005  # Small wick
                    low_price = new_price * 0.995   # Small wick
                
                decimals = CandlestickService._get_price_decimals(new_price)
                
                candle_data = CandlestickData(
                    asset=asset,
                    timestamp=current_time,
                    open_price=round(Decimal(str(open_price)), decimals),
                    high_price=round(Decimal(str(high_price)), decimals),
                    low_price=round(Decimal(str(low_price)), decimals),
                    close_price=round(Decimal(str(close_price)), decimals),
                    volume=Decimal('3000'),  # Higher volume for admin changes
                    interval='1min'
                )
                
                candle_data.save()
                logger.info(f"Created admin price change candle for {asset.symbol}: {last_close} -> {new_price}")
                
        except Exception as e:
            logger.error(f"Error creating admin price change candle: {e}")

    @staticmethod
    def _ensure_base_data(asset):
        """Ensure we have base 1-minute data for the asset"""
        latest_candle = CandlestickData.get_latest_candle(asset, '1min')
        current_time = timezone.now()
        
        if not latest_candle:
            # Generate initial historical data
            CandlestickService._generate_initial_data(asset)
        else:
            # Add missing candles since last update
            time_diff = current_time - latest_candle.timestamp
            minutes_diff = int(time_diff.total_seconds() / 60)
            
            if minutes_diff > 1:  # Only add if more than 1 minute gap
                CandlestickService._add_missing_candles_realistic(asset, latest_candle.timestamp)

    @staticmethod
    def _add_missing_candles_realistic(asset, last_timestamp):
        """
        Add missing candles with proper price progression toward current asset price
        """
        current_time = timezone.now().replace(second=0, microsecond=0)
        
        time_diff = current_time - last_timestamp
        minutes_to_add = int(time_diff.total_seconds() / 60)
        
        if minutes_to_add <= 0:
            return
            
        minutes_to_add = min(minutes_to_add, 1440)  # Max 24 hours
        
        last_candle = CandlestickData.objects.filter(
            asset=asset, interval='1min'
        ).order_by('-timestamp').first()
        
        if not last_candle:
            return
            
        # Get current asset price - FIXED: Ensure we're getting the actual current price
        current_asset_price = float(asset.price_usd) if asset.price_usd else None
        if not current_asset_price or current_asset_price <= 0:
            logger.warning(f"Invalid current asset price for {asset.symbol}: {current_asset_price}")
            current_asset_price = float(last_candle.close_price)  # Fallback to last candle close
        
        last_close = float(last_candle.close_price)
        
        # Check if there was a major price change
        if last_close > 0:
            price_change_ratio = abs(current_asset_price - last_close) / last_close
            is_major_change = price_change_ratio > 0.05  # 5% threshold
        else:
            is_major_change = False
        
        # Volatility calculation
        volatility = CandlestickService._get_realistic_volatility(current_asset_price)
        
        candles_to_create = []
        current_price = last_close
        momentum = 0.0
        
        # Enhanced variation logic for post-change behavior
        if is_major_change:
            volatility *= 4.0  # Increase volatility after major moves
            logger.info(f"Major price change detected ({price_change_ratio:.2%}), increasing volatility")
        
        for i in range(1, minutes_to_add + 1):
            candle_time = last_timestamp + timedelta(minutes=i)
            
            # Progress toward current asset price
            progress = i / minutes_to_add
            target_price = last_close + (current_asset_price - last_close) * progress
            
            # Add momentum and mean reversion
            momentum += random.uniform(-volatility * 1.5, volatility * 1.5)
            momentum *= 0.90  # Momentum decay
            
            open_price = current_price
            
            # Base movement toward target
            base_movement = (target_price - current_price) * random.uniform(0.3, 0.8)
            
            # Momentum effect
            momentum_effect = momentum * current_price
            
            # Noise for realistic variation
            trend_noise = random.gauss(0, volatility * 0.8) * current_price
            micro_noise = random.uniform(-volatility * 0.5, volatility * 0.5) * current_price
            
            # Occasional larger moves
            if random.random() < 0.15:  # 15% chance of larger move
                spike_factor = random.uniform(-3.0, 3.0)
                trend_noise += spike_factor * volatility * current_price
            
            close_price = current_price + base_movement + momentum_effect + trend_noise + micro_noise
            
            # Generate OHLC with minimal wicks
            price_range = abs(close_price - open_price)
            
            # Reduced wick multipliers
            wick_multiplier = random.uniform(0.1, 0.3)
            
            if close_price > open_price:
                high_extension = price_range * random.uniform(0.05, 0.15) * wick_multiplier
                low_extension = price_range * random.uniform(0.02, 0.08) * wick_multiplier
                
                high_price = max(open_price, close_price) + high_extension
                low_price = min(open_price, close_price) - low_extension
            else:
                high_extension = price_range * random.uniform(0.02, 0.08) * wick_multiplier
                low_extension = price_range * random.uniform(0.05, 0.15) * wick_multiplier
                
                high_price = max(open_price, close_price) + high_extension
                low_price = min(open_price, close_price) - low_extension
            
            # Ensure proper OHLC relationships
            high_price = max(high_price, open_price, close_price)
            low_price = min(low_price, open_price, close_price)
            
            # Prevent extreme deviations
            max_deviation = current_asset_price * 0.5
            min_price_limit = max(0.0001, current_asset_price - max_deviation)
            max_price_limit = current_asset_price + max_deviation
            
            close_price = max(min_price_limit, min(close_price, max_price_limit))
            high_price = max(min_price_limit, min(high_price, max_price_limit))
            low_price = max(min_price_limit, min(low_price, max_price_limit))
            
            decimals = CandlestickService._get_price_decimals(current_asset_price)
            
            candle_data = {
                'asset': asset,
                'timestamp': candle_time,
                'open_price': round(Decimal(str(open_price)), decimals),
                'high_price': round(Decimal(str(high_price)), decimals),
                'low_price': round(Decimal(str(low_price)), decimals),
                'close_price': round(Decimal(str(close_price)), decimals),
                'volume': Decimal(str(random.randint(800, 4000))),
                'interval': '1min'
            }
            
            candles_to_create.append(candle_data)
            current_price = close_price
        
        # Bulk create
        if candles_to_create:
            try:
                CandlestickData.objects.bulk_create([
                    CandlestickData(**candle) for candle in candles_to_create
                ], ignore_conflicts=True)
                
                logger.info(f"Added {len(candles_to_create)} realistic candles for {asset.symbol}")
            except Exception as e:
                logger.error(f"Error adding candles: {e}")
                raise

    @staticmethod
    def _update_latest_candle_realistic(asset, chart_data, interval_minutes):
        """
        Update latest candle to reflect current asset price
        """
        if not chart_data:
            return chart_data
            
        # Get current asset price - FIXED: Ensure we're using actual current price
        current_asset_price = float(asset.price_usd) if asset.price_usd else None
        if not current_asset_price or current_asset_price <= 0:
            logger.warning(f"Invalid current asset price for update: {current_asset_price}")
            return chart_data
        
        latest_candle = chart_data[-1]
        
        if latest_candle['close'] > 0:
            price_diff = abs(current_asset_price - latest_candle['close']) / latest_candle['close']
            
            # Only update if there's a reasonable difference
            if price_diff > 0.001:  # 0.1% threshold
                volatility = CandlestickService._get_realistic_volatility(current_asset_price)
                
                # Add some variation but keep it close to actual price
                variation = random.gauss(0, volatility * 0.5) * current_asset_price
                adjusted_price = current_asset_price + variation
                
                # Limit variation to 5% of actual price
                max_variation = current_asset_price * 0.05
                adjusted_price = max(
                    current_asset_price - max_variation,
                    min(adjusted_price, current_asset_price + max_variation)
                )
                
                # Update OHLC
                latest_candle['high'] = max(latest_candle['high'], adjusted_price, current_asset_price)
                latest_candle['low'] = min(latest_candle['low'], adjusted_price, current_asset_price)
                latest_candle['close'] = adjusted_price
                
                logger.info(f"Updated latest candle for {asset.symbol} to price: {adjusted_price}")
        
        return chart_data

    @staticmethod 
    def _get_realistic_volatility(price):
        """
        Get volatility based on price level
        """
        price_magnitude = abs(price) if price else 1.0
        if price_magnitude < 0.00001:
            return 0.02   
        elif price_magnitude < 0.0001:
            return 0.015   
        elif price_magnitude < 0.001:
            return 0.012   
        elif price_magnitude < 0.01:
            return 0.01   
        elif price_magnitude < 0.1:
            return 0.008   
        elif price_magnitude < 1:
            return 0.006  
        else:
            return 0.005   

    @staticmethod
    def _get_aggregated_data(asset, interval_minutes, candle_count):
        """Get aggregated data for the requested interval - NO CACHING"""
        logger.info(f"Starting aggregation for {asset.symbol}, interval: {interval_minutes}min, count: {candle_count}")
        
        # Always fetch fresh data from database
        all_candles = list(CandlestickData.objects.filter(
            asset=asset,
            interval='1min'
        ).order_by('timestamp'))
        
        if not all_candles or len(all_candles) < interval_minutes:
            logger.warning(f"Insufficient candle data for aggregation: {len(all_candles) if all_candles else 0}")
            return []
        
        latest_candle = all_candles[-1]
        latest_time = latest_candle.timestamp
        
        total_minutes_needed = interval_minutes * candle_count
        start_time = latest_time - timedelta(minutes=total_minutes_needed)
        
        relevant_candles = [
            candle for candle in all_candles
            if candle.timestamp >= start_time
        ]
        
        if not relevant_candles:
            relevant_candles = all_candles[-total_minutes_needed:] if len(all_candles) >= total_minutes_needed else all_candles
        
        if not relevant_candles:
            return []
        
        first_time = relevant_candles[0].timestamp
        last_time = relevant_candles[-1].timestamp
        current_period_start = CandlestickService._round_to_interval(first_time, interval_minutes)
        
        if current_period_start < first_time:
            current_period_start += timedelta(minutes=interval_minutes)
        
        aggregated_candles = []
        
        while current_period_start <= last_time and len(aggregated_candles) < candle_count:
            period_end = current_period_start + timedelta(minutes=interval_minutes)
            
            period_candles = [
                candle for candle in relevant_candles
                if current_period_start <= candle.timestamp < period_end
            ]
            
            if period_candles:
                open_price = float(period_candles[0].open_price)
                close_price = float(period_candles[-1].close_price)
                high_price = max(float(c.high_price) for c in period_candles)
                low_price = min(float(c.low_price) for c in period_candles)
                
                # Ensure no zero or negative prices
                if all(p > 0 for p in [open_price, high_price, low_price, close_price]):
                    aggregated_candles.append({
                        'time': int(current_period_start.timestamp()),
                        'open': round(open_price, 8),
                        'high': round(high_price, 8),
                        'low': round(low_price, 8),
                        'close': round(close_price, 8)
                    })
                else:
                    logger.warning(f"Skipping candle with invalid prices: O:{open_price}, H:{high_price}, L:{low_price}, C:{close_price}")
            
            current_period_start = period_end
        
        result = aggregated_candles[-candle_count:] if len(aggregated_candles) > candle_count else aggregated_candles
        logger.info(f"Aggregation complete: {len(result)} candles generated")
        return result

    @staticmethod
    def _generate_initial_data(asset, days_back=7):
        """Generate initial historical data"""
        current_time = timezone.now()
        start_time = current_time - timedelta(days=days_back)
        start_time = start_time.replace(second=0, microsecond=0)
        current_time = current_time.replace(second=0, microsecond=0)
        
        # Use current asset price as the ending point
        current_asset_price = float(asset.price_usd) if asset.price_usd else 1.0
        if current_asset_price <= 0:
            current_asset_price = 1.0
            
        prev_price = float(asset.prev_price_usd) if asset.prev_price_usd and asset.prev_price_usd > 0 else current_asset_price
        
        total_minutes = int((current_time - start_time).total_seconds() / 60)
        
        price_path = CandlestickService._create_price_path(
            prev_price * 0.9,  # Start from 90% of previous price
            current_asset_price,  # End at current asset price
            total_minutes
        )
        
        batch_size = 1000
        candles_created = 0
        
        for batch_start in range(0, total_minutes, batch_size):
            batch_end = min(batch_start + batch_size, total_minutes)
            batch_candles = []
            
            momentum = 0.0
            volatility = CandlestickService._get_realistic_volatility(current_asset_price)
            
            for i in range(batch_start, batch_end):
                candle_time = start_time + timedelta(minutes=i)
                target_price = price_path[i]
                
                if i == 0:
                    open_price = price_path[0]
                elif batch_candles:
                    open_price = batch_candles[-1]['close_price_float']
                else:
                    prev_candle = CandlestickData.objects.filter(
                        asset=asset,
                        interval='1min',
                        timestamp__lt=candle_time
                    ).order_by('-timestamp').first()
                    
                    if prev_candle:
                        open_price = float(prev_candle.close_price)
                    else:
                        open_price = target_price
                
                # Add momentum and noise
                momentum += random.uniform(-volatility * 0.4, volatility * 0.4)
                momentum *= 0.95  # Momentum decay
                
                price_change = (target_price - open_price) * random.uniform(0.4, 0.9)
                momentum_effect = momentum * open_price
                noise = random.gauss(0, volatility * 0.15) * open_price
                
                close_price = open_price + price_change + momentum_effect + noise
                
                # Generate minimal wicks
                if close_price > open_price:
                    high_price = max(open_price, close_price) * random.uniform(1.001, 1.01)
                    low_price = min(open_price, close_price) * random.uniform(0.99, 0.999)
                else:
                    high_price = max(open_price, close_price) * random.uniform(1.001, 1.005)
                    low_price = min(open_price, close_price) * random.uniform(0.995, 0.99)
                
                # Ensure proper OHLC relationships
                high_price = max(high_price, open_price, close_price)
                low_price = min(low_price, open_price, close_price)
                
                # Prevent extreme deviations
                max_price = current_asset_price * 1.5
                min_price = max(0.0001, current_asset_price * 0.3)
                
                close_price = max(min_price, min(close_price, max_price))
                high_price = max(min_price, min(high_price, max_price))
                low_price = max(min_price, min(low_price, max_price))
                
                decimals = CandlestickService._get_price_decimals(current_asset_price)
                
                candle_data = {
                    'asset': asset,
                    'timestamp': candle_time,
                    'open_price': round(Decimal(str(open_price)), decimals),
                    'high_price': round(Decimal(str(high_price)), decimals),
                    'low_price': round(Decimal(str(low_price)), decimals),
                    'close_price': round(Decimal(str(close_price)), decimals),
                    'volume': Decimal('1000'),
                    'interval': '1min',
                    'close_price_float': close_price
                }
                
                batch_candles.append(candle_data)
            
            if batch_candles:
                try:
                    candle_objects = []
                    for candle in batch_candles:
                        candle_copy = candle.copy()
                        candle_copy.pop('close_price_float', None)
                        candle_objects.append(CandlestickData(**candle_copy))
                    
                    CandlestickData.objects.bulk_create(candle_objects, ignore_conflicts=True)
                    candles_created += len(candle_objects)
                    
                except Exception as e:
                    logger.error(f"Error creating batch: {e}")
                    raise
        
        logger.info(f"Created {candles_created} initial candles for {asset.symbol}")

    # Utility methods remain the same
    @staticmethod
    def _get_interval_minutes(interval):
        mapping = {
            '1min': 1, '5min': 5, '15min': 15, '1hr': 60, '1hour': 60
        }
        return mapping.get(interval.lower(), 15)
    
    @staticmethod
    def _get_default_candle_count(interval):
        mapping = {
            '1min': 240, '5min': 288, '15min': 96, '1hr': 48
        }
        return mapping.get(interval, 96)
    
    @staticmethod
    def _get_price_decimals(price):
        if price < 0.000001: return 10
        elif price < 0.00001: return 9
        elif price < 0.0001: return 8
        elif price < 0.001: return 6
        elif price < 0.01: return 5
        elif price < 0.1: return 4
        elif price < 1: return 3
        else: return 2
    
    @staticmethod
    def _round_to_interval(timestamp, interval_minutes):
        minutes = timestamp.minute
        rounded_minutes = (minutes // interval_minutes) * interval_minutes
        return timestamp.replace(minute=rounded_minutes, second=0, microsecond=0)
    
    @staticmethod
    def _create_price_path(start_price, end_price, steps):
        if steps <= 1:
            return [end_price]
        
        path = []
        num_segments = max(3, steps // 120)
        segment_size = steps // num_segments
        
        waypoints = [start_price]
        for i in range(1, num_segments):
            progress = i / num_segments
            base_point = start_price + (end_price - start_price) * progress
            noise_factor = 0.03 * (1 - progress * 0.3)
            noise = random.uniform(-noise_factor, noise_factor) * base_point
            waypoints.append(base_point + noise)
        waypoints.append(end_price)
        
        for i in range(len(waypoints) - 1):
            start_wp = waypoints[i]
            end_wp = waypoints[i + 1]
            
            if i == len(waypoints) - 2:
                segment_steps = steps - len(path)
            else:
                segment_steps = segment_size
            
            for j in range(segment_steps):
                if len(path) >= steps:
                    break
                
                t = j / max(1, segment_steps - 1)
                smooth_t = t * t * (3.0 - 2.0 * t)
                interpolated = start_wp + (end_wp - start_wp) * smooth_t
                
                variation = random.uniform(-0.002, 0.002) * interpolated
                path.append(interpolated + variation)
        
        while len(path) < steps:
            path.append(end_price)
            
        return path[:steps]