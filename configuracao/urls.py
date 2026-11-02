from django.urls import path
from . import views

urlpatterns = [
    path("setup/", views.setup, name="setup"),
    path("expirado/", views.trial_expirado, name="trial_expirado"),
]
