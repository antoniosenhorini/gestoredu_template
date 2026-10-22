"""
URL configuration for colegio project.
"""
from django.conf import settings
from django.conf.urls.static import static
from django.contrib import admin
from django.shortcuts import render
from django.urls import include, path


def superuser_admin_permission(request):
    return request.user.is_active and request.user.is_superuser


admin.site.has_permission = superuser_admin_permission


def permission_denied_view(request, exception=None):
    return render(request, '403.html', status=403)


handler403 = permission_denied_view


urlpatterns = [
    path('admin/', admin.site.urls),
    # O configuracao.urls já tem o 'setup/' dentro dele. 
    # Colocamos ele primeiro para não ser engolido pelo apps.urls:
    path('', include('configuracao.urls')),  
    path('', include('apps.urls')),
]

urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
urlpatterns += static(settings.STATIC_URL, document_root=settings.STATIC_ROOT)