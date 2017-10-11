from django.conf.urls import patterns, include, url
from django.contrib import admin

from app.apiconfig import apis
admin.autodiscover()

urlpatterns = patterns('',
                       # Examples:
                       # url(r'^$', 'x.views.home', name='home'),
                       # url(r'^blog/', include('blog.urls')),
                       url(r'^', include('landingpage.urls')),
                       url(r'^accounts/', include('allauth.urls')),
                       url(r'^admin/', include(admin.site.urls)),
                       )

urlpatterns += patterns('', *apis.urls)
