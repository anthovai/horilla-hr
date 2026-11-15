from django.contrib import messages
from django.shortcuts import render
from django.utils.translation import gettext as __
from django.utils.translation import gettext_lazy as _

from horilla.decorators import login_required, superuser_required

from .forms import LDAPSettingsForm
from .models import LDAPSettings

# Create your views here.


@login_required
@superuser_required
def ldap_settings_view(request):
    from horilla.tenancy import is_multi_tenant

    if is_multi_tenant() and request.method != "POST":
        messages.info(
            request,
            _(
                "LDAP sign-in is not available on hosted OrvaHR. These settings "
                "are used only to import users from your directory."
            ),
        )
    settings = LDAPSettings.objects.first()
    if request.method == "POST":
        form = LDAPSettingsForm(request.POST, instance=settings)
        if form.is_valid():
            form.save()
            messages.success(request, _("Configuration updated successfully."))
            return render(request, "ldap_settings.html", {"form": form})
    else:
        form = LDAPSettingsForm(instance=settings)

    return render(request, "ldap_settings.html", {"form": form})
