from django.urls import path
from . import views

urlpatterns = [
    path('', views.movie_list, name='movie_list'),
    path('<int:movie_id>/theaters', views.theater_list, name='theater_list'),
    path('theater/<int:theater_id>/seats/book/', views.book_seats, name='book_seats'),
    path('<int:movie_id>/detail/', views.movie_detail, name='movie_detail'),
    path('theater/<int:theater_id>/payment/', views.initiate_payment, name='initiate_payment'),
    path('theater/<int:theater_id>/payment/process/', views.process_payment, name='process_payment'),
    path('payment/success/', views.payment_success, name='payment_success'),
    path('payment/failed/', views.payment_failed, name='payment_failed'),
    path('payment/webhook/', views.payment_webhook, name='payment_webhook'),
    path('admin-dashboard/', views.admin_dashboard, name='admin_dashboard'),
    path('booking/<int:booking_id>/cancel/', views.cancel_booking, name='cancel_booking'),
    path('event/<int:event_id>/book/', views.book_event, name='book_event'),
    path('event/<int:event_id>/payment/', views.process_event_payment, name='process_event_payment'),
    path('plays/', views.play_list, name='play_list'),
    path('play/<int:play_id>/', views.play_detail, name='play_detail'),
    path('play/<int:play_id>/payment/', views.process_play_payment, name='process_play_payment'),       
    path('theater/<int:theater_id>/seats/status/', views.seat_status_api, name='seat_status_api'),
]
