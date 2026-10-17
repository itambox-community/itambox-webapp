from django.http import HttpResponse
from django.urls import path


def index(request):
    return HttpResponse("fixture plugin")


urlpatterns = [path("", index, name="index")]
