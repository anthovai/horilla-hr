from django.contrib import messages
from django.core.exceptions import ValidationError
from django.core.validators import URLValidator
from django.db.models import Count, Q
from django.http import HttpResponseNotAllowed
from django.shortcuts import get_object_or_404, redirect, render
from django.utils.translation import gettext as _

from horilla.decorators import login_required, permission_required
from line_integration import linking
from line_integration.client import LineClient
from line_integration.dispatch import integration_enabled
from line_integration.forms import LineChannelForm
from line_integration.models import LineChannel, LineMessageLog, LineUserLink


# Admin settings --------------------------------------------------------


@login_required
@permission_required("line_integration.view_linechannel")
def line_settings(request):
    channels = LineChannel.objects.select_related("company").annotate(
        linked=Count("links", filter=Q(links__is_active=True))
    )
    rows = [
        {
            "channel": c,
            "token": c.masked_access_token(),
            "secret": c.masked_channel_secret(),
            "webhook_url": c.webhook_url(request),
        }
        for c in channels
    ]
    return render(
        request,
        "line_integration/settings.html",
        {
            "rows": rows,
            "enabled": integration_enabled(),
            "logs": LineMessageLog.objects.select_related("employee")[:20],
            "my_link": _my_link(request),
        },
    )


def _channel_form(request, instance=None):
    form = LineChannelForm(request.POST or None, instance=instance)
    if request.method == "POST" and form.is_valid():
        channel = form.save(commit=False)
        if not channel.public_base_url:
            guess = request.build_absolute_uri("/").rstrip("/")
            try:
                URLValidator()(guess)
                channel.public_base_url = guess
            except ValidationError:
                pass  # e.g. an internal hostname; admin can fill it in
        channel.save()
        messages.success(request, _("LINE channel saved."))
        return redirect("line-settings")
    return render(
        request, "line_integration/channel_form.html", {"form": form, "instance": instance}
    )


@login_required
@permission_required("line_integration.add_linechannel")
def channel_create(request):
    return _channel_form(request)


@login_required
@permission_required("line_integration.change_linechannel")
def channel_update(request, pk):
    return _channel_form(request, get_object_or_404(LineChannel, pk=pk))


@login_required
@permission_required("line_integration.delete_linechannel")
def channel_delete(request, pk):
    if request.method != "POST":
        return HttpResponseNotAllowed(["POST"])
    get_object_or_404(LineChannel, pk=pk).delete()
    messages.success(request, _("LINE channel deleted."))
    return redirect("line-settings")


def _my_link(request):
    employee = getattr(request.user, "employee_get", None)
    if employee is None:
        return None
    return LineUserLink.objects.select_related("channel").filter(employee=employee).first()


@login_required
@permission_required("line_integration.change_linechannel")
def send_test_message(request):
    if request.method != "POST":
        return HttpResponseNotAllowed(["POST"])
    link = _my_link(request)
    if link is None or not link.is_active:
        messages.error(request, _("Your account is not linked to LINE yet."))
    elif LineClient(link.channel).push_text(
        link.line_user_id,
        _("OrvaHR test message: LINE notifications are working."),
        employee=link.employee,
        event="test",
    ):
        messages.success(request, _("Test message sent."))
    else:
        messages.error(request, _("Test message failed. See the delivery log below."))
    return redirect("line-settings")


# Employee self-service -----------------------------------------------


@login_required
def my_line_link(request):
    employee = request.user.employee_get
    new_code = None
    expires_at = None
    if request.method == "POST":
        action = request.POST.get("action")
        if action == "unlink":
            LineUserLink.objects.filter(employee=employee).delete()
            messages.success(request, _("LINE disconnected."))
            return redirect("line-my-link")
        if action == "generate":
            try:
                new_code, code_obj = linking.generate_code(employee)
                expires_at = code_obj.expires_at
            except linking.RateLimited:
                messages.error(request, _("Too many codes requested. Please try again later."))
            except linking.NoChannel:
                messages.error(request, _("LINE is not set up for your company yet."))
    link = LineUserLink.objects.select_related("channel").filter(employee=employee).first()
    channel = link.channel if link else linking.channel_for_employee(employee)
    response = render(
        request,
        "line_integration/my_link.html",
        {
            "link": link,
            "channel": channel,
            "enabled": integration_enabled(),
            "new_code": new_code,
            "expires_at": expires_at,
            "ttl_minutes": int(linking.CODE_TTL.total_seconds() // 60),
        },
    )
    if new_code:
        response["Cache-Control"] = "no-store"
    return response
