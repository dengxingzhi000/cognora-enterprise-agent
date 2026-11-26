from typing import Annotated

from fastapi import APIRouter, HTTPException, Path, Request

from agent.assistance.features import (
    AssistanceFeedbackRequest,
    AssistanceOutcomeRequest,
    AssistancePredictRequest,
)
from agent.assistance.model import AssistanceModelError
from agent.assistance.service import (
    AssistanceKnowledgeNotFound,
    AssistancePredictionNotFound,
    AssistanceStoreError,
)
from security.internal_auth import trusted_identity


def create_assistance_router(get_service):
    router = APIRouter(prefix="/assistance", tags=["assistance"])

    @router.post("/predict")
    def predict(request: AssistancePredictRequest, http_request: Request):
        identity = trusted_identity(http_request)
        tenant_id = identity.tenant_id if identity else "t1"
        user_id = identity.user_id if identity else "u1"
        role = identity.role if identity else "employee"
        try:
            return get_service().predict(tenant_id, user_id, role, request)
        except AssistanceModelError:
            raise HTTPException(
                status_code=503,
                detail={
                    "code": "ASSISTANCE_MODEL_UNAVAILABLE",
                    "message": "Assistance model unavailable",
                },
            ) from None
        except AssistanceStoreError:
            raise HTTPException(
                status_code=503,
                detail={
                    "code": "ASSISTANCE_STORE_UNAVAILABLE",
                    "message": "Assistance store unavailable",
                },
            ) from None

    @router.post("/predictions/{prediction_id}/feedback")
    def feedback(
        prediction_id: Annotated[str, Path(pattern=r"[A-Za-z0-9._-]{1,128}")],
        request: AssistanceFeedbackRequest,
        http_request: Request,
    ):
        identity = trusted_identity(http_request)
        tenant_id = identity.tenant_id if identity else "t1"
        user_id = identity.user_id if identity else "u1"
        try:
            return get_service().feedback(tenant_id, user_id, prediction_id, request)
        except AssistancePredictionNotFound:
            raise HTTPException(
                status_code=404,
                detail={
                    "code": "ASSISTANCE_PREDICTION_NOT_FOUND",
                    "message": "Prediction not found",
                },
            ) from None
        except AssistanceKnowledgeNotFound:
            raise HTTPException(
                status_code=404,
                detail={
                    "code": "ASSISTANCE_KNOWLEDGE_NOT_FOUND",
                    "message": "Knowledge article not found",
                },
            ) from None
        except AssistanceStoreError:
            raise HTTPException(
                status_code=503,
                detail={
                    "code": "ASSISTANCE_STORE_UNAVAILABLE",
                    "message": "Assistance store unavailable",
                },
            ) from None

    @router.post("/outcomes")
    def outcome(request: AssistanceOutcomeRequest, http_request: Request):
        identity = trusted_identity(http_request)
        if identity is None:
            raise HTTPException(
                status_code=401,
                detail={
                    "code": "ASSISTANCE_INTERNAL_IDENTITY_REQUIRED",
                    "message": "Trusted internal identity required",
                },
            )
        try:
            return get_service().record_outcome(
                identity.tenant_id, identity.user_id, request
            )
        except AssistancePredictionNotFound:
            raise HTTPException(
                status_code=404,
                detail={
                    "code": "ASSISTANCE_PREDICTION_NOT_FOUND",
                    "message": "Prediction not found",
                },
            ) from None
        except AssistanceStoreError:
            raise HTTPException(
                status_code=503,
                detail={
                    "code": "ASSISTANCE_STORE_UNAVAILABLE",
                    "message": "Assistance store unavailable",
                },
            ) from None

    @router.get("/model/status")
    def model_status(http_request: Request):
        identity = trusted_identity(http_request)
        if identity is None:
            raise HTTPException(
                status_code=401,
                detail={
                    "code": "ASSISTANCE_INTERNAL_IDENTITY_REQUIRED",
                    "message": "Trusted internal identity required",
                },
            )
        try:
            return get_service().model_status(identity.tenant_id)
        except AssistanceModelError:
            raise HTTPException(
                status_code=503,
                detail={
                    "code": "ASSISTANCE_MODEL_UNAVAILABLE",
                    "message": "Assistance model unavailable",
                },
            ) from None

    @router.get("/dashboard/data")
    def dashboard_data(http_request: Request):
        identity = trusted_identity(http_request)
        if identity is None:
            raise HTTPException(
                status_code=401,
                detail={
                    "code": "ASSISTANCE_INTERNAL_IDENTITY_REQUIRED",
                    "message": "Trusted internal identity required",
                },
            )
        try:
            return get_service().dashboard_data(identity.tenant_id)
        except AssistanceModelError:
            raise HTTPException(
                status_code=503,
                detail={
                    "code": "ASSISTANCE_MODEL_UNAVAILABLE",
                    "message": "Assistance model unavailable",
                },
            ) from None
        except AssistanceStoreError:
            raise HTTPException(
                status_code=503,
                detail={
                    "code": "ASSISTANCE_STORE_UNAVAILABLE",
                    "message": "Assistance store unavailable",
                },
            ) from None

    return router
