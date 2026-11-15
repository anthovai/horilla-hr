from django.contrib import messages
from django.contrib.auth.decorators import permission_required
from django.http import QueryDict
from django.shortcuts import redirect, render
from django.utils.decorators import method_decorator
from django.utils.translation import gettext_lazy as _
from django.views.decorators.csrf import csrf_exempt
from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from base.models import Company
from facedetection.forms import FaceDetectionSetupForm
from horilla.decorators import hx_request_required, login_required
from horilla.http.response import HorillaRedirect

from .serializers import *

# PDPA risk: this legacy flow stores one raw photo per employee in MEDIA_ROOT
# (biometric data, PDPA s.26) with no consent record and no retention rule,
# and trusts the mobile app's own match result. orvahr_face replaces it with
# consented, encrypted templates verified server-side. The upload refuses
# (HTTP 410) in SaaS mode and for companies that enabled orvahr_face; other
# single-tenant companies keep the old behaviour. Stored photos are removed
# with ``manage.py purge_legacy_face_photos``.
LEGACY_RETIRED_MESSAGE = _(
    "The legacy face photo upload is retired. Use Face Check-in in OrvaHR instead."
)


def legacy_upload_refused(company):
    from django.apps import apps

    from horilla.tenancy import is_multi_tenant

    if not apps.is_installed("orvahr_face"):
        return is_multi_tenant()
    from orvahr_face.hooks import legacy_upload_refused as refused

    return refused(company)


class FaceDetectionConfigAPIView(APIView):
    permission_classes = [IsAuthenticated]

    def get_company(self, request):
        try:
            company = request.user.employee_get.get_company()
            return company
        except Exception as e:
            raise serializers.ValidationError(e)

    def get_facedetection(self, request):
        company = self.get_company(request)
        try:
            facedetection = FaceDetection.objects.get_or_create(company_id=company)
            return facedetection
        except Exception as e:
            raise serializers.ValidationError(e)

    def get(self, request):
        serializer = FaceDetectionSerializer(self.get_facedetection(request)[0])
        return Response(serializer.data, status=status.HTTP_200_OK)

    @method_decorator(
        permission_required("facedetection.add_facedetection", raise_exception=True),
        name="dispatch",
    )
    def post(self, request):
        data = request.data
        if isinstance(data, QueryDict):
            data = data.dict()
        data["company_id"] = self.get_company(request).id
        serializer = FaceDetectionSerializer(data=data)
        if serializer.is_valid():
            serializer.save()
            return Response(serializer.data, status=status.HTTP_201_CREATED)
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

    @method_decorator(
        permission_required("facedetection.change_facedetection", raise_exception=True),
        name="dispatch",
    )
    def put(self, request):
        data = request.data
        serializer = FaceDetectionSerializer(
            self.get_facedetection(request)[0], data=data
        )
        if serializer.is_valid():
            serializer.save()
            return Response(serializer.data, status=status.HTTP_200_OK)
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

    @method_decorator(
        permission_required("facedetection.delete_facedetection", raise_exception=True),
        name="dispatch",
    )
    def delete(self, request):
        self.get_facedetection(request).delete()
        return Response(
            {"message": "Facedetection deleted successfully"}, status=status.HTTP_200_OK
        )


class EmployeeFaceDetectionGetPostAPIView(APIView):
    permission_classes = [IsAuthenticated]

    @method_decorator(csrf_exempt)
    def dispatch(self, *args, **kwargs):
        return super().dispatch(*args, **kwargs)

    def get_company(self, request):
        try:
            company = request.user.employee_get.get_company()
            return company
        except Exception as e:
            raise serializers.ValidationError(e)

    def get_facedetection(self, request):
        company = self.get_company(request)
        try:
            facedetection = FaceDetection.objects.get(company_id=company)
            return facedetection
        except Exception as e:
            raise serializers.ValidationError(e)

    def post(self, request):
        employee = getattr(request.user, "employee_get", None)
        company = employee.get_company() if employee is not None else None
        if legacy_upload_refused(company):
            return Response(
                {"error": str(LEGACY_RETIRED_MESSAGE)}, status=status.HTTP_410_GONE
            )
        if self.get_facedetection(request).start:
            employee_id = request.user.employee_get.id
            data = request.data
            if isinstance(data, QueryDict):
                data = data.dict()
            data["employee_id"] = employee_id
            instance = EmployeeFaceDetection.objects.filter(
                employee_id=employee_id
            ).first()
            serializer = EmployeeFaceDetectionSerializer(data=data, instance=instance)
            if serializer.is_valid():
                serializer.save()
                return Response(serializer.data, status=status.HTTP_201_CREATED)
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)
        raise serializers.ValidationError(_("Facedetection not yet started.."))


def get_company(request):
    try:
        selected_company = request.session.get("selected_company")
        if selected_company == "all":
            return None
        company = Company.objects.get(id=selected_company)
        return company
    except Exception as e:
        raise serializers.ValidationError(e)


def get_facedetection(request):
    company = get_company(request)
    try:
        location = FaceDetection.objects.get(company_id=company)
        return location
    except Exception as e:
        raise serializers.ValidationError(e)


@login_required
@permission_required("geofencing.add_localbackup")
@hx_request_required
def face_detection_config(request):
    try:
        form = FaceDetectionSetupForm(instance=get_facedetection(request))
    except:
        form = FaceDetectionSetupForm()

    if request.method == "POST":
        try:
            form = FaceDetectionSetupForm(
                request.POST, instance=get_facedetection(request)
            )
        except:
            form = FaceDetectionSetupForm(request.POST)
        if form.is_valid():
            facedetection = form.save(
                commit=False,
            )
            facedetection.company_id = get_company(request)
            facedetection.save()
            messages.success(request, _("facedetection config created successfully."))
        else:
            messages.info(request, _("Not valid"))
    return render(request, "face_config.html", {"form": form})


@login_required
@permission_required("facedetection.add_facedetection")
def enable_disable_face_detection(request):
    """
    Enables or disables the face detection feature for the active company,
    mirroring the Track Late Come & Early Out enable/disable workflow.
    """
    if request.method == "POST":
        selected_company = request.session.get("selected_company")
        company = None
        if selected_company and selected_company != "all":
            company = Company.objects.filter(id=selected_company).first()
        if request.POST.get("start") and legacy_upload_refused(company):
            messages.error(request, LEGACY_RETIRED_MESSAGE)
            return HorillaRedirect(request)
        instance, _created = FaceDetection.objects.get_or_create(company_id=company)
        instance.start = bool(request.POST.get("start"))
        instance.save()
        message = _("enabled") if instance.start else _("disabled")
        messages.success(request, _("Face detection {} successfully").format(message))
    return HorillaRedirect(request)
