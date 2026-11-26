"""SCM Tool适配：按平台网关契约执行只读查询，写工具明确不支持。"""
import os
import uuid
from urllib.parse import quote

from pydantic import BaseModel

from agent.tools.registry import Tool

from .auth import to_scm_tenant
from .client import ScmClient


class ToolBusinessError(RuntimeError):
    """可结构化识别的工具失败；消息只含固定摘要，不含响应或传输异常原文。"""

    def __init__(self, tool: str, code: str, message: str, *, status_code: int | None = None):
        self.tool = tool
        self.code = code
        self.message = message
        self.status_code = status_code
        super().__init__(f"{tool}: {code} ({message})")


def _use_mock() -> bool:
    return not os.environ.get("SCM_GATEWAY_URL")


class OrderGetArgs(BaseModel):
    order_id: str | None = None
    order_no: str = "20260915001"
    tenant_id: str = "t1"


class InventoryArgs(BaseModel):
    sku: str = "SKU-001"
    tenant_id: str = "t1"


class SalesArgs(BaseModel):
    range: str = "7d"
    tenant_id: str = "t1"


class SupplierArgs(BaseModel):
    supplier_id: str = "SP-001"
    tenant_id: str = "t1"


class PurchaseCreateArgs(BaseModel):
    sku: str
    quantity: int = 1
    tenant_id: str = "t1"


class OrderCancelArgs(BaseModel):
    order_no: str
    tenant_id: str = "t1"


class StockAdjustArgs(BaseModel):
    sku: str
    quantity: int
    tenant_id: str = "t1"


def _mock_order(args: dict):
    return f"row1: order_no={args.get('order_no', '20260915001')} status=PAID amount=6230 (mock)"


def _mock_inventory(args: dict):
    return f"sku={args.get('sku', 'SKU-001')} stock=128 warehouse=WH-01 (mock)"


def _mock_sales(args: dict):
    return f"sales range={args.get('range', '7d')} gmv=123456 orders=321 (mock)"


def _mock_supplier(args: dict):
    sid = args.get("supplier_id", "SP-001")
    return f"supplier={sid} rating=B credit_score=72 risk=medium (mock)"


def _tenant_headers(args: dict) -> dict[str, str]:
    return {"X-Tenant-Id": to_scm_tenant(args.get("tenant_id", "t1"))}


def _read(client, tool: str, method: str, path: str, *, params=None, json=None,
          headers: dict[str, str] | None = None):
    kwargs = {"headers": headers}
    if params is not None:
        kwargs["params"] = params
    try:
        if method == "GET":
            status, body = client.get(path, **kwargs)
        else:
            status, body = client.post(path, json=json, **kwargs)
    except Exception as exc:
        raise ToolBusinessError(tool, "SCM_TRANSPORT_ERROR", "SCM request failed") from exc

    if status == 0:
        raise ToolBusinessError(tool, "SCM_TRANSPORT_ERROR", "SCM request failed", status_code=status)
    if status == 401:
        raise ToolBusinessError(tool, "SCM_AUTHENTICATION_FAILED", "SCM authentication failed",
                                status_code=status)
    if status != 200:
        raise ToolBusinessError(tool, "SCM_HTTP_ERROR", "SCM request failed", status_code=status)
    # 平台 ApiResponse.fail 也使用 HTTP 200，业务 code 必须单独校验。
    if isinstance(body, dict) and body.get("code") not in (None, 200):
        raise ToolBusinessError(tool, "SCM_BUSINESS_ERROR", "SCM request failed", status_code=status)
    return body


def _unsupported(tool: str):
    def fail(args: dict):
        raise ToolBusinessError(tool, "UNSUPPORTED_TOOL", "write tool is not implemented")
    return fail


def register_scm_tools(registry, client=None):
    if _use_mock() and client is None:
        registry.register(Tool("scm.order.get", "查SCM订单(只读)", _mock_order, OrderGetArgs))
        registry.register(Tool("scm.inventory.query", "查SCM库存(只读)", _mock_inventory, InventoryArgs))
        registry.register(Tool("scm.sales.report", "查销售聚合(只读)", _mock_sales, SalesArgs))
        registry.register(Tool("scm.supplier.get", "查SCM供应商风险(只读)", _mock_supplier, SupplierArgs))
    else:
        c = client or ScmClient.from_env()

        def _order_get(args: dict):
            args = args or {}
            headers = _tenant_headers(args)
            order_id = args.get("order_id")
            if order_id:
                try:
                    order_id = str(uuid.UUID(str(order_id)))
                except ValueError as exc:
                    raise ToolBusinessError("scm.order.get", "INVALID_ARGUMENT", "order_id must be a UUID") from exc
                return _read(c, "scm.order.get", "GET",
                             f"/scm-order/api/v1/orders/{order_id}", headers=headers)
            order_no = args.get("order_no", "")
            if not order_no:
                raise ToolBusinessError("scm.order.get", "INVALID_ARGUMENT", "order_id or order_no is required")
            return _read(c, "scm.order.get", "POST", "/scm-order/api/v1/orders/query",
                         json={"orderNo": order_no, "pageNum": 1, "pageSize": 1}, headers=headers)

        def _inv_query(args: dict):
            args = args or {}
            return _read(c, "scm.inventory.query", "POST",
                         "/platform-inventory/api/v1/inventory/query",
                         json={"skuIds": [args.get("sku", "")], "page": 1, "size": 20},
                         headers=_tenant_headers(args))

        def _sales(args: dict):
            args = args or {}
            ranges = {
                "today": "TODAY", "7d": "LAST_7_DAYS", "30d": "LAST_30_DAYS",
                "TODAY": "TODAY", "LAST_7_DAYS": "LAST_7_DAYS", "LAST_30_DAYS": "LAST_30_DAYS",
            }
            selected = ranges.get(args.get("range", "7d"))
            if selected is None:
                raise ToolBusinessError("scm.sales.report", "INVALID_ARGUMENT", "unsupported sales range")
            return _read(c, "scm.sales.report", "GET",
                         "/scm-order/api/v1/orders/dashboard/summary",
                         params={"range": selected, "compare": "NONE"},
                         headers=_tenant_headers(args))

        def _supplier_get(args: dict):
            args = args or {}
            supplier_id = quote(str(args.get("supplier_id", "")), safe="")
            return _read(c, "scm.supplier.get", "GET",
                         f"/scm-supplier/api/v1/suppliers/{supplier_id}",
                         headers=_tenant_headers(args))

        registry.register(Tool("scm.order.get", "按订单UUID ID或订单号查SCM订单(只读)", _order_get, OrderGetArgs))
        registry.register(Tool("scm.inventory.query", "查SCM库存(只读)", _inv_query, InventoryArgs))
        registry.register(Tool("scm.sales.report", "查销售聚合(只读)", _sales, SalesArgs))
        registry.register(Tool("scm.supplier.get", "查SCM供应商风险(只读)", _supplier_get, SupplierArgs))

    registry.register(Tool("scm.purchase.create", "建采购单(未实现，审批后仍报错)",
                           _unsupported("scm.purchase.create"), PurchaseCreateArgs))
    registry.register(Tool("scm.order.cancel", "取消SCM订单(未实现，审批后仍报错)",
                           _unsupported("scm.order.cancel"), OrderCancelArgs))
    registry.register(Tool("scm.stock.adjust", "调整SCM库存(未实现，审批后仍报错)",
                           _unsupported("scm.stock.adjust"), StockAdjustArgs))
    return registry
