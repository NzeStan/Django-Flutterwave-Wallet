from django.contrib import admin
from django.urls import include, path

urlpatterns = [
    path('admin/', admin.site.urls),
    path('wallet/', include('flutterwave_wallet.urls')),
]
