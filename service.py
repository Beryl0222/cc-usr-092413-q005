"""银发服务体验认证的运行入口。

除原有健康检查外，挂载“认证范围拆分与承接”领域服务：

* ``POST /rpc``：执行命令，请求体为
  ``{"command": <命令名>, "params": {...}, "idempotency_key"?: <键>}``；
* ``GET /certificates/<id>``、``/certificates/<id>/versions``、
  ``/certificates/<id>/lineage``：证书视图、版本与溯源；
* ``GET /splits/<id>``：拆分工作台（冻结快照、承接声明、证据候选、阻断）；
* ``GET /consumers/<id>``、``/promotions/<id>``：消费者处置与宣传引用；
* ``GET /facts``：追加事实审计流。
"""

import argparse
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import unquote, urlparse

from certification import CertificationService, DomainError

SERVICE_ID = "silver-service-certification"
SERVICE_NAME = "银发服务体验认证"

# 进程内单一领域实例；事实仅保存在内存中，供本地联调与契约测试使用。
default_service = CertificationService()


def health_payload():
    """返回稳定的服务身份信息。"""
    return {"status": "ok", "service": SERVICE_ID, "name": SERVICE_NAME}


class Handler(BaseHTTPRequestHandler):
    """提供健康检查、拆分承接命令入口与只读查询路由。"""

    def __init__(self, *args, service=None, **kwargs):
        self._service = service or default_service
        super().__init__(*args, **kwargs)

    # ------------------------------------------------------------- GET 查询

    def do_GET(self):
        path = urlparse(self.path).path
        try:
            if path == "/health":
                self._write_json(200, health_payload())
                return
            if path == "/facts":
                self._write_json(200, {"facts": self._service.facts()})
                return
            segments = [unquote(s) for s in path.strip("/").split("/") if s]
            if len(segments) == 2 and segments[0] == "certificates":
                self._write_json(200, self._service.get_certificate(segments[1]))
                return
            if len(segments) == 3 and segments[0] == "certificates" and segments[2] == "versions":
                self._write_json(200, self._service.get_versions(segments[1]))
                return
            if len(segments) == 3 and segments[0] == "certificates" and segments[2] == "lineage":
                self._write_json(200, self._service.lineage(segments[1]))
                return
            if len(segments) == 2 and segments[0] == "splits":
                self._write_json(200, self._service.get_split(segments[1]))
                return
            if len(segments) == 2 and segments[0] == "consumers":
                self._write_json(200, self._service.get_consumer(segments[1]))
                return
            if len(segments) == 2 and segments[0] == "promotions":
                self._write_json(200, self._service.get_promotion(segments[1]))
                return
            self.send_error(404)
        except DomainError as error:
            self._write_domain_error(error)

    # ------------------------------------------------------------- POST 命令

    def do_POST(self):
        path = urlparse(self.path).path
        if path != "/rpc":
            self.send_error(404)
            return
        try:
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if length else b"{}"
            try:
                request = json.loads(raw.decode("utf-8") or "{}")
            except (UnicodeDecodeError, json.JSONDecodeError):
                raise DomainError("INVALID_JSON", "请求体必须是 UTF-8 JSON", http_status=400)
            if not isinstance(request, dict) or "command" not in request:
                raise DomainError("MISSING_COMMAND", "请求体需要包含 command 字段", http_status=400)
            params = request.get("params") or {}
            if not isinstance(params, dict):
                raise DomainError("INVALID_PARAMS", "params 必须是对象", http_status=400)
            result = self._service.command(
                request["command"],
                params,
                idempotency_key=request.get("idempotency_key"),
            )
            self._write_json(200, {"ok": True, "result": result})
        except DomainError as error:
            self._write_domain_error(error)

    # ------------------------------------------------------------- 输出工具

    def _write_json(self, status, payload):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _write_domain_error(self, error):
        self._write_json(
            error.http_status,
            {"ok": False, "error": {"code": error.code, "message": error.message, "details": error.details}},
        )

    def log_message(self, *_args):
        return


def build_handler(service):
    """构造绑定指定领域实例（便于测试隔离）的 Handler 类。"""

    class BoundHandler(Handler):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, service=service, **kwargs)

    return BoundHandler


def main():
    parser = argparse.ArgumentParser(description=SERVICE_NAME)
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    if args.check:
        assert health_payload()["service"] == SERVICE_ID
        # 领域内核可实例化且事实流初始为空。
        assert CertificationService().facts() == []
        print("基础检查通过")
        return
    ThreadingHTTPServer(("0.0.0.0", args.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
