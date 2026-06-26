from django.shortcuts import render, redirect, get_object_or_404
from django.contrib import messages
from django.views.decorators.csrf import csrf_exempt
from django.http import JsonResponse, HttpResponse
from django.conf import settings
from django.urls import reverse
from django.utils import timezone
from django.utils.dateparse import parse_date
import uuid
import hmac
import hashlib
import json
import logging

logger = logging.getLogger('movies.tasks')

from .models import Movie, Theater, Seat, Booking, SeatLock
from .utils import validate_youtube_url, get_youtube_thumbnail
from django.contrib.auth.decorators import login_required
from django.db import IntegrityError
from django.core.paginator import Paginator
from django.db.models import Count
from django_q.tasks import async_task


def _parse_show_date(value):
    if value:
        parsed_date = parse_date(value)
        if parsed_date:
            return parsed_date
    return timezone.localdate()


def _get_show_date(request):
    return _parse_show_date(
        request.POST.get('show_date')
        or request.GET.get('date')
        or request.session.get('show_date')
    )


def _book_seats_url(theater_id, show_date):
    return f"{reverse('book_seats', args=[theater_id])}?date={show_date.isoformat()}"


def _build_seat_statuses(theaters, seats, show_date, user):
    booked_seat_ids = set(
        Booking.objects.filter(
            theater=theaters,
            show_date=show_date,
            seat__in=seats,
        ).values_list('seat_id', flat=True)
    )
    locked_seat_ids = set(
        SeatLock.objects.filter(
            seat__in=seats,
            show_date=show_date,
            is_active=True,
            expires_at__gt=timezone.now(),
        ).exclude(user=user).values_list('seat_id', flat=True)
    )

    return [
        {
            'seat': seat,
            'is_booked': seat.id in booked_seat_ids,
            'is_locked': seat.id in locked_seat_ids,
        }
        for seat in seats
    ]


def _build_seat_layout(seats_with_status):
    """
    Groups seats into category -> rows -> left/center/right blocks.
    Normal rows: 4 left, 12 center, 4 right (20 total).
    VIP rows: 10 seats, center block only.
    """
    from collections import OrderedDict
    import re

    categories = OrderedDict()

    for item in seats_with_status:
        seat = item['seat']
        category = seat.category
        match = re.match(r'([A-Za-z]+)(\d+)', seat.seat_number)
        row_label = match.group(1) if match else seat.seat_number
        seat_num = int(match.group(2)) if match else 0

        categories.setdefault(category, OrderedDict())
        categories[category].setdefault(row_label, [])
        categories[category][row_label].append((seat_num, item))

    layout = []
    for category, rows in categories.items():
        row_list = []
        for row_label, seat_items in rows.items():
            seat_items.sort(key=lambda x: x[0])
            ordered_items = [s[1] for s in seat_items]
            total = len(ordered_items)

            if category == 'vip':
                left_block = []
                center_block = ordered_items
                right_block = []
            else:
                left_count = 4 if total >= 8 else 0
                right_count = 4 if total >= 8 else 0
                left_block = ordered_items[:left_count]
                right_block = ordered_items[-right_count:] if right_count else []
                center_block = ordered_items[left_count: total - right_count] if right_count else ordered_items[left_count:]

            row_list.append({
                'row_label': row_label,
                'left': left_block,
                'center': center_block,
                'right': right_block,
            })

        sample_seat = rows[next(iter(rows))][0][1]['seat']
        layout.append({
            'category': category,
            'category_display': sample_seat.get_category_display(),
            'price': sample_seat.get_price(),
            'rows': row_list,
        })

    return layout


def _render_seat_selection(request, theaters, seats, show_date, error=None):
    seats_with_status = _build_seat_statuses(
        theaters,
        seats,
        show_date,
        request.user,
    )
    context = {
        'theaters': theaters,
        'seats': seats,
        'seats_with_status': seats_with_status,
        'seat_layout': _build_seat_layout(seats_with_status),
        'show_date': show_date.isoformat(),
    }
    if error:
        context['error'] = error
    return render(request, 'movies/seat_selection.html', context)


def movie_list(request):
    movies = Movie.objects.all()

    search = request.GET.get('search', '')
    if search:
        movies = movies.filter(name__icontains=search)

    genres = request.GET.getlist('genre')
    if genres:
        movies = movies.filter(genre__in=genres)

    languages = request.GET.getlist('language')
    if languages:
        movies = movies.filter(language__in=languages)

    sort_by = request.GET.get('sort_by', '')
    if sort_by in ['rating', '-rating', 'name', '-name']:
        movies = movies.order_by(sort_by)

    genre_count_raw = Movie.objects.values('genre').annotate(count=Count('id'))
    genre_count_map = {}
    for item in genre_count_raw:
        genre_count_map[item['genre']] = item['count']

    language_count_raw = Movie.objects.values('language').annotate(count=Count('id'))
    language_count_map = {}
    for item in language_count_raw:
        language_count_map[item['language']] = item['count']

    genre_list = []
    for value, label in Movie.GENRE_CHOICES:
        count = genre_count_map.get(value, 0)
        genre_list.append((value, label, count))

    language_list = []
    for value, label in Movie.LANGUAGE_CHOICES:
        count = language_count_map.get(value, 0)
        language_list.append((value, label, count))

    paginator = Paginator(movies, 6)
    page_number = request.GET.get('page')
    page_obj = paginator.get_page(page_number)

    query_params = request.GET.copy()
    if 'page' in query_params:
        query_params.pop('page')
    query_string = query_params.urlencode()

    return render(request, 'movies/movie_list.html', {
        'movies': page_obj,
        'page_obj': page_obj,
        'total_count': movies.count(),
        'genre_list': genre_list,
        'language_list': language_list,
        'selected_genres': genres,
        'selected_languages': languages,
        'selected_sort': sort_by,
        'search': search,
        'query_string': query_string,
    })


def theater_list(request, movie_id):
    from datetime import date, timedelta
    movie = get_object_or_404(Movie, id=movie_id)
    theater = Theater.objects.filter(movie=movie)

    today = date.today()
    dates = []
    for i in range(7):
        d = today + timedelta(days=i)
        dates.append({
            'date': d.strftime('%Y-%m-%d'),
            'day': d.strftime('%a'),
            'num': d.strftime('%d'),
            'month': d.strftime('%b'),
            'is_today': i == 0,
        })

    selected_date = request.GET.get('date', today.strftime('%Y-%m-%d'))

    return render(request, 'movies/theater_list.html', {
        'movie': movie,
        'theaters': theater,
        'today': today.strftime('%Y-%m-%d'),
        'dates': dates,
        'selected_date': selected_date,
    })


@login_required(login_url='/login/')
def book_seats(request, theater_id):
    from django.db import transaction

    theaters = get_object_or_404(Theater, id=theater_id)
    seats = Seat.objects.filter(theater=theaters)
    show_date = _get_show_date(request)

    SeatLock.objects.filter(
        is_active=True,
        expires_at__lt=timezone.now()
    ).update(is_active=False)

    if request.method == 'POST':
        selected_seats = request.POST.getlist('seats')
        error_seats = []
        locked_seats = []

        if not selected_seats:
            return _render_seat_selection(
                request,
                theaters,
                seats,
                show_date,
                "No seat selected",
            )

        try:
            with transaction.atomic():
                for seat_id in selected_seats:
                    seat = Seat.objects.select_for_update().get(
                        id=seat_id,
                        theater=theaters
                    )

                    if Booking.objects.filter(
                        seat=seat,
                        theater=theaters,
                        show_date=show_date,
                    ).exists():
                        error_seats.append(seat.seat_number)
                        continue

                    existing_lock = SeatLock.objects.filter(
                        seat=seat,
                        show_date=show_date,
                        is_active=True,
                        expires_at__gt=timezone.now()
                    ).exclude(user=request.user).first()

                    if existing_lock:
                        error_seats.append(seat.seat_number)
                        continue

                    from datetime import timedelta
                    lock, created = SeatLock.objects.update_or_create(
                        seat=seat,
                        show_date=show_date,
                        defaults={
                            'user': request.user,
                            'is_active': True,
                            'expires_at': timezone.now() + timedelta(minutes=2),
                        }
                    )
                    locked_seats.append(seat)

        except Exception as e:
            logger.error(f'Seat locking error: {str(e)}')
            return _render_seat_selection(
                request,
                theaters,
                seats,
                show_date,
                "Seat locking failed. Please try again.",
            )

        if error_seats:
            error_message = f"Seats {', '.join(error_seats)} are already booked or locked!"
            return _render_seat_selection(
                request,
                theaters,
                seats,
                show_date,
                error_message,
            )

        if locked_seats:
            request.session['selected_seats'] = selected_seats
            request.session['theater_id'] = theater_id
            request.session['show_date'] = show_date.isoformat()

            ticket_price = getattr(settings, 'TICKET_PRICE', 250)
            total_amount = len(selected_seats) * ticket_price
            idempotency_key = str(uuid.uuid4())

            request.session['total_amount'] = str(total_amount)
            request.session['idempotency_key'] = idempotency_key

            return render(request, 'movies/payment.html', {
                'theaters': theaters,
                'selected_seats': selected_seats,
                'total_amount': total_amount,
                'idempotency_key': idempotency_key,
                'ticket_price': ticket_price,
                'num_seats': len(selected_seats),
                'show_date': show_date.isoformat(),
                'publishable_key': settings.STRIPE_PUBLISHABLE_KEY,
                'lock_expires_in': 120,
            })

    return _render_seat_selection(request, theaters, seats, show_date)


def movie_detail(request, movie_id):
    movie = get_object_or_404(Movie, id=movie_id)
    safe_embed_url = validate_youtube_url(movie.trailer_url)
    thumbnail_url = get_youtube_thumbnail(movie.trailer_url)
    return render(request, 'movies/movie_detail.html', {
        'movie': movie,
        'safe_embed_url': safe_embed_url,
        'thumbnail_url': thumbnail_url,
    })


@login_required(login_url='/login/')
def initiate_payment(request, theater_id):
    from datetime import timedelta
    from django.db import transaction

    theaters = get_object_or_404(Theater, id=theater_id)
    show_date = _get_show_date(request)

    if request.method == 'POST':
        selected_seats = request.POST.getlist('seats')

        if not selected_seats:
            messages.error(request, 'No seats selected!')
            return redirect(_book_seats_url(theater_id, show_date))

        try:
            with transaction.atomic():
                for seat_id in selected_seats:
                    seat = Seat.objects.select_for_update().get(
                        id=seat_id,
                        theater=theaters
                    )
                    if Booking.objects.filter(
                        seat=seat,
                        theater=theaters,
                        show_date=show_date,
                    ).exists():
                        messages.error(request, f'Seat {seat.seat_number} already booked!')
                        return redirect(_book_seats_url(theater_id, show_date))

                    existing_lock = SeatLock.objects.filter(
                        seat=seat,
                        show_date=show_date,
                        is_active=True,
                        expires_at__gt=timezone.now()
                    ).exclude(user=request.user).first()

                    if existing_lock:
                        messages.error(request, f'Seat {seat.seat_number} is locked by another user!')
                        return redirect(_book_seats_url(theater_id, show_date))

                    SeatLock.objects.update_or_create(
                        seat=seat,
                        show_date=show_date,
                        defaults={
                            'user': request.user,
                            'is_active': True,
                            'expires_at': timezone.now() + timedelta(minutes=2),
                        }
                    )

        except Exception as e:
            logger.error(f'Seat lock error: {str(e)}')
            messages.error(request, 'Failed to lock seats. Please try again.')
            return redirect(_book_seats_url(theater_id, show_date))

        ticket_price = getattr(settings, 'TICKET_PRICE', 250)
        total_amount = len(selected_seats) * ticket_price
        idempotency_key = str(uuid.uuid4())

        request.session['selected_seats'] = selected_seats
        request.session['total_amount'] = str(total_amount)
        request.session['idempotency_key'] = idempotency_key
        request.session['theater_id'] = theater_id
        request.session['show_date'] = show_date.isoformat()

        return render(request, 'movies/payment.html', {
            'theaters': theaters,
            'selected_seats': selected_seats,
            'total_amount': total_amount,
            'idempotency_key': idempotency_key,
            'ticket_price': ticket_price,
            'num_seats': len(selected_seats),
            'show_date': show_date.isoformat(),
            'publishable_key': settings.STRIPE_PUBLISHABLE_KEY,
            'lock_expires_in': 120,
        })

    return redirect(_book_seats_url(theater_id, show_date))


@login_required(login_url='/login/')
def process_payment(request, theater_id):
    from .models import Payment
    from django.db import transaction

    if request.method == 'POST':
        theaters = get_object_or_404(Theater, id=theater_id)
        selected_seats = request.session.get('selected_seats', [])
        total_amount = request.session.get('total_amount', 0)
        idempotency_key = request.session.get('idempotency_key')
        show_date = _get_show_date(request)

        if not selected_seats:
            messages.error(request, 'No seats selected!')
            return redirect(_book_seats_url(theater_id, show_date))

        existing_payment = Payment.objects.filter(
            idempotency_key=idempotency_key
        ).first()

        if existing_payment and existing_payment.status == 'success':
            messages.warning(request, 'Payment already processed!')
            return redirect('payment_success')

        card_number = request.POST.get('card_number', '')

        if card_number and len(card_number.replace(' ', '')) == 16:
            payment_id = f'pay_{uuid.uuid4().hex[:16]}'
            signature = hmac.new(
                settings.STRIPE_SECRET_KEY.encode(),
                f'{idempotency_key}|{payment_id}'.encode(),
                hashlib.sha256
            ).hexdigest()

            error_seats = []
            successful_bookings = []

            try:
                with transaction.atomic():
                    for seat_id in selected_seats:
                        seat = Seat.objects.select_for_update().get(
                            id=seat_id,
                            theater=theaters,
                        )

                        if Booking.objects.filter(
                            seat=seat,
                            theater=theaters,
                            show_date=show_date,
                        ).exists():
                            error_seats.append(seat.seat_number)
                            continue

                        lock_exists = SeatLock.objects.filter(
                            seat=seat,
                            show_date=show_date,
                            user=request.user,
                            is_active=True,
                            expires_at__gt=timezone.now(),
                        ).exists()

                        if not lock_exists:
                            error_seats.append(seat.seat_number)
                            continue

                        try:
                            booking = Booking.objects.create(
                                user=request.user,
                                seat=seat,
                                movie=theaters.movie,
                                theater=theaters,
                                show_date=show_date,
                            )
                            successful_bookings.append(booking)
                        except IntegrityError:
                            error_seats.append(seat.seat_number)
            except Seat.DoesNotExist:
                return redirect('payment_failed')

            if successful_bookings:
                payment = Payment.objects.create(
                    idempotency_key=idempotency_key,
                    user=request.user,
                    booking=successful_bookings[0],
                    amount=total_amount,
                    status='success',
                    payment_id=payment_id,
                    payment_signature=signature,
                )

                for seat_id in selected_seats:
                    try:
                        seat = Seat.objects.get(id=seat_id)
                        SeatLock.objects.filter(
                            seat=seat,
                            show_date=show_date,
                            user=request.user
                        ).update(is_active=False)
                    except Exception as e:
                        logger.error(f'Error releasing lock: {str(e)}')

                for booking in successful_bookings:
                    async_task(
                        'movies.tasks.send_booking_confirmation_email',
                        booking.id
                    )

                request.session.pop('selected_seats', None)
                request.session.pop('total_amount', None)
                request.session.pop('idempotency_key', None)
                request.session.pop('show_date', None)
                request.session.pop('theater_id', None)
                request.session['payment_id'] = payment_id
                request.session['payment_amount'] = str(total_amount)

                return redirect('payment_success')
            else:
                last_booking = Booking.objects.filter(user=request.user).last()
                if last_booking:
                    Payment.objects.create(
                        idempotency_key=idempotency_key,
                        user=request.user,
                        booking=last_booking,
                        amount=total_amount,
                        status='failed',
                    )
                return redirect('payment_failed')
        else:
            return redirect('payment_failed')

    return redirect('book_seats', theater_id=theater_id)


@login_required(login_url='/login/')
def payment_success(request):
    payment_id = request.session.get('payment_id', 'N/A')
    payment_amount = request.session.get('payment_amount', '0')
    return render(request, 'movies/payment_success.html', {
        'payment_id': payment_id,
        'payment_amount': payment_amount,
    })


@login_required(login_url='/login/')
def payment_failed(request):
    return render(request, 'movies/payment_failed.html')


@csrf_exempt
def payment_webhook(request):
    from .models import Payment

    if request.method == 'POST':
        try:
            payload = json.loads(request.body)
            event_type = payload.get('type')
            payment_id = payload.get('payment_id')

            logger.info(f'Webhook received: {event_type} for {payment_id}')

            existing = Payment.objects.filter(
                payment_id=payment_id
            ).first()

            if event_type == 'payment.success':
                if existing:
                    existing.status = 'success'
                    existing.save()
                    logger.info(f'Payment {payment_id} marked success')

            elif event_type == 'payment.failed':
                if existing:
                    existing.status = 'failed'
                    existing.save()
                    logger.warning(f'Payment {payment_id} marked failed')

            elif event_type == 'payment.cancelled':
                if existing:
                    existing.status = 'cancelled'
                    existing.save()
                    logger.warning(f'Payment {payment_id} cancelled')

            return HttpResponse(status=200)

        except Exception as e:
            logger.error(f'Webhook error: {str(e)}')
            return HttpResponse(status=400)

    return HttpResponse(status=405)

from django.contrib.admin.views.decorators import staff_member_required
from django.core.cache import cache
from django.db.models import Sum, Count, Avg
from django.db.models.functions import TruncDay, TruncWeek, TruncMonth, TruncHour
from datetime import datetime, timedelta


@staff_member_required
def admin_dashboard(request):
    """
    Advanced Admin Analytics Dashboard.
    Role-based authentication — staff only.
    Uses database-level aggregation — never loads entire dataset.
    Results cached for 5 minutes to prevent performance degradation.
    """
    from .models import Payment

    revenue_data = cache.get('dashboard_revenue')
    if not revenue_data:
        today = datetime.now().date()
        week_ago = today - timedelta(days=7)
        month_ago = today - timedelta(days=30)

        daily_revenue = Payment.objects.filter(
            status='success',
            created_at__date=today
        ).aggregate(total=Sum('amount'))['total'] or 0

        weekly_revenue = Payment.objects.filter(
            status='success',
            created_at__date__gte=week_ago
        ).aggregate(total=Sum('amount'))['total'] or 0

        monthly_revenue = Payment.objects.filter(
            status='success',
            created_at__date__gte=month_ago
        ).aggregate(total=Sum('amount'))['total'] or 0

        total_revenue = Payment.objects.filter(
            status='success'
        ).aggregate(total=Sum('amount'))['total'] or 0

        revenue_data = {
            'daily': daily_revenue,
            'weekly': weekly_revenue,
            'monthly': monthly_revenue,
            'total': total_revenue,
        }
        cache.set('dashboard_revenue', revenue_data, 300)

    popular_movies = cache.get('dashboard_popular_movies')
    if not popular_movies:
        popular_movies = Booking.objects.values(
            'movie__name'
        ).annotate(
            booking_count=Count('id')
        ).order_by('-booking_count')[:10]
        cache.set('dashboard_popular_movies', list(popular_movies), 300)

    busiest_theaters = cache.get('dashboard_busiest_theaters')
    if not busiest_theaters:
        busiest_theaters = Booking.objects.values(
            'theater__name'
        ).annotate(
            booking_count=Count('id'),
            occupancy=Count('seat')
        ).order_by('-booking_count')[:10]
        cache.set('dashboard_busiest_theaters', list(busiest_theaters), 300)

    peak_hours = cache.get('dashboard_peak_hours')
    if not peak_hours:
        peak_hours = Booking.objects.annotate(
            hour=TruncHour('booked_at')
        ).values('hour').annotate(
            count=Count('id')
        ).order_by('-count')[:10]
        cache.set('dashboard_peak_hours', list(peak_hours), 300)

    cancellation_data = cache.get('dashboard_cancellation')
    if not cancellation_data:
        from .models import Payment
        total_payments = Payment.objects.count()
        failed_payments = Payment.objects.filter(
            status__in=['failed', 'cancelled']
        ).count()
        success_payments = Payment.objects.filter(
            status='success'
        ).count()

        cancellation_rate = 0
        if total_payments > 0:
            cancellation_rate = round(
                (failed_payments / total_payments) * 100, 2
            )

        cancellation_data = {
            'total': total_payments,
            'success': success_payments,
            'failed': failed_payments,
            'rate': cancellation_rate,
        }
        cache.set('dashboard_cancellation', cancellation_data, 300)

    total_bookings = Booking.objects.count()
    total_users = Booking.objects.values('user').distinct().count()
    total_movies = Movie.objects.count()
    total_theaters = Theater.objects.count()

    context = {
        'revenue': revenue_data,
        'popular_movies': popular_movies,
        'busiest_theaters': busiest_theaters,
        'peak_hours': peak_hours,
        'cancellation': cancellation_data,
        'total_bookings': total_bookings,
        'total_users': total_users,
        'total_movies': total_movies,
        'total_theaters': total_theaters,
    }

    return render(request, 'movies/admin_dashboard.html', context)


@login_required(login_url='/login/')
def cancel_booking(request, booking_id):
    """
    Cancel a booking — releases seat and updates payment status.
    This affects cancellation rate shown on admin dashboard.
    """
    from .models import Payment

    booking = get_object_or_404(Booking, id=booking_id, user=request.user)

    try:
        seat = booking.seat
        seat.is_booked = False
        seat.save()

        payment = Payment.objects.filter(booking=booking).first()
        if payment:
            payment.status = 'cancelled'
            payment.save()

        booking.delete()

        from django.core.cache import cache
        cache.delete('dashboard_revenue')
        cache.delete('dashboard_popular_movies')
        cache.delete('dashboard_busiest_theaters')
        cache.delete('dashboard_peak_hours')
        cache.delete('dashboard_cancellation')

        messages.success(request, 'Booking cancelled successfully!')

    except Exception as e:
        logger.error(f'Cancel booking error: {str(e)}')
        messages.error(request, 'Failed to cancel booking.')

    return redirect('profile')


@login_required(login_url='/login/')
def book_event(request, event_id):
    from .models import Event, EventBooking
    from django.db import transaction

    event = get_object_or_404(Event, id=event_id)

    if request.method == 'POST':
        quantity = int(request.POST.get('quantity', 1))

        if quantity < 1:
            messages.error(request, 'Invalid quantity!')
            return redirect('home')

        if quantity > event.tickets_remaining():
            messages.error(request, f'Only {event.tickets_remaining()} tickets remaining!')
            return redirect('home')

        total_amount = quantity * event.ticket_price

        request.session['event_id'] = event_id
        request.session['event_quantity'] = quantity
        request.session['event_total_amount'] = str(total_amount)
        request.session['event_idempotency_key'] = str(uuid.uuid4())

        return render(request, 'movies/event_payment.html', {
            'event': event,
            'quantity': quantity,
            'total_amount': total_amount,
        })

    return render(request, 'movies/event_detail.html', {'event': event})


@login_required(login_url='/login/')
def process_event_payment(request, event_id):
    from .models import Event, EventBooking

    event = get_object_or_404(Event, id=event_id)

    if request.method == 'POST':
        quantity = request.session.get('event_quantity', 1)
        total_amount = request.session.get('event_total_amount', 0)
        idempotency_key = request.session.get('event_idempotency_key')

        existing = EventBooking.objects.filter(idempotency_key=idempotency_key).first()
        if existing and existing.status == 'success':
            messages.warning(request, 'Booking already processed!')
            return redirect('payment_success')

        card_number = request.POST.get('card_number', '')

        if card_number and len(card_number.replace(' ', '')) == 16:
            if quantity <= event.tickets_remaining():
                booking = EventBooking.objects.create(
                    user=request.user,
                    event=event,
                    quantity=quantity,
                    total_amount=total_amount,
                    status='success',
                    idempotency_key=idempotency_key,
                )

                request.session.pop('event_id', None)
                request.session.pop('event_quantity', None)
                request.session.pop('event_total_amount', None)
                request.session.pop('event_idempotency_key', None)
                request.session['payment_id'] = f'evt_{booking.id}'
                request.session['payment_amount'] = str(total_amount)

                return redirect('payment_success')
            else:
                return redirect('payment_failed')
        else:
            return redirect('payment_failed')

    return redirect('home')


def play_list(request):
    from .models import Play
    plays = Play.objects.filter(play_date__gte=timezone.localdate())
    return render(request, 'movies/play_list.html', {'plays': plays})


@login_required(login_url='/login/')
def play_detail(request, play_id):
    from .models import Play, PlayBooking

    play = get_object_or_404(Play, id=play_id)

    if request.method == 'POST':
        quantity = int(request.POST.get('quantity', 1))

        if quantity < 1:
            messages.error(request, 'Invalid quantity!')
            return redirect('play_list')

        if quantity > play.tickets_remaining():
            messages.error(request, f'Only {play.tickets_remaining()} tickets remaining!')
            return redirect('play_list')

        total_amount = quantity * play.ticket_price

        request.session['play_id'] = play_id
        request.session['play_quantity'] = quantity
        request.session['play_total_amount'] = str(total_amount)
        request.session['play_idempotency_key'] = str(uuid.uuid4())

        return render(request, 'movies/play_payment.html', {
            'play': play,
            'quantity': quantity,
            'total_amount': total_amount,
        })

    return render(request, 'movies/play_detail.html', {'play': play})


@login_required(login_url='/login/')
def process_play_payment(request, play_id):
    from .models import Play, PlayBooking

    play = get_object_or_404(Play, id=play_id)

    if request.method == 'POST':
        quantity = request.session.get('play_quantity', 1)
        total_amount = request.session.get('play_total_amount', 0)
        idempotency_key = request.session.get('play_idempotency_key')

        existing = PlayBooking.objects.filter(idempotency_key=idempotency_key).first()
        if existing and existing.status == 'success':
            messages.warning(request, 'Booking already processed!')
            return redirect('payment_success')

        card_number = request.POST.get('card_number', '')

        if card_number and len(card_number.replace(' ', '')) == 16:
            if quantity <= play.tickets_remaining():
                booking = PlayBooking.objects.create(
                    user=request.user,
                    play=play,
                    quantity=quantity,
                    total_amount=total_amount,
                    status='success',
                    idempotency_key=idempotency_key,
                )

                request.session.pop('play_id', None)
                request.session.pop('play_quantity', None)
                request.session.pop('play_total_amount', None)
                request.session.pop('play_idempotency_key', None)
                request.session['payment_id'] = f'play_{booking.id}'
                request.session['payment_amount'] = str(total_amount)

                return redirect('payment_success')
            else:
                return redirect('payment_failed')
        else:
            return redirect('payment_failed')

    return redirect('play_list')