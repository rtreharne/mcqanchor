from django.urls import path

from . import views

app_name = "website"

urlpatterns = [
    path("", views.home, name="home"),
    path("api/product-chat/", views.product_chat, name="product_chat"),
]
