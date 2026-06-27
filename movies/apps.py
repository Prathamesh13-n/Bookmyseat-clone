from django.apps import AppConfig

class MoviesConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'movies'

    def ready(self):
        """
        Start APScheduler when Django starts.
        Automatically releases expired seat locks every 30 seconds.
        """
        from apscheduler.schedulers.background import BackgroundScheduler
        from apscheduler.triggers.interval import IntervalTrigger
        import atexit

        scheduler = BackgroundScheduler()
        scheduler.add_job(
            func=self.release_locks,
            trigger=IntervalTrigger(seconds=30),
            id='release_seat_locks',
            name='Release expired seat locks every 30 seconds',
            replace_existing=True,
        )
        scheduler.start()

        # shut down scheduler when app exits
        atexit.register(lambda: scheduler.shutdown())

    def release_locks(self):
        try:
            from movies.tasks import release_expired_seat_locks
            release_expired_seat_locks()
        except Exception:
            pass