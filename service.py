"""银发服务体验认证的运行入口。

在 /health 之外暴露认证范围拆分与承接的 JSON 接口；每个 HTTP 进程持有一个
内存型 CertificationService 实例。
"""

import argparse
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from certification import CertificationError, CertificationService

SERVICE_ID = "silver-service-certification"
SERVICE_NAME = "银发服务体验认证"

# 路由表：方法 + 路径 -> 服务方法
POST_ROUTES = {
    "/certificates": "register_certificate",
    "/evidence": "add_evidence",
    "/events": "add_severe_event",
    "/consumers": "register_consumer",
    "/splits/freeze": "freeze_certificate",
    "/splits": "initiate_split",
    "/splits/declarations": "submit_declaration",
    "/splits/candidates": "inheritance_candidates",
    "/reviews/confirm": "confirm_review",
    "/splits/issue": "issue",
    "/certificates/remediation": "submit_remediation",
    "/certificates/revoke": "revoke_scope",
    "/citations": "register_citation",
}

GET_ROUTES = {
    "/certificates": "list_certificates",
}

# 路径中带标识的只读接口
DYNAMIC_GET = (
    ("/certificates/", "_get_certificate_view"),
    ("/splits/", "_get_split_view"),
    ("/trace/", "_get_trace_view"),
)


def health_payload():
    """返回稳定的服务身份信息。"""
    return {"status": "ok", "service": SERVICE_ID, "name": SERVICE_NAME}


def build_service():
    return CertificationService()


class Handler(BaseHTTPRequestHandler):
    """提供健康检查与认证拆分承接接口。"""

    service = build_service()

    def do_GET(self):
        if self.path == "/health":
            self._write_json(200, health_payload())
            return
        path = self.path.split("?", 1)[0]
        action = GET_ROUTES.get(path)
        if action:
            try:
                self._write_json(200, getattr(self.service, action)())
            except CertificationError as error:
                self._write_json(422, {"error": str(error)})
            return
        for prefix, method_name in DYNAMIC_GET:
            if path.startswith(prefix):
                identifier = path[len(prefix):].strip("/")
                if identifier:
                    getattr(self, method_name)(identifier)
                    return
        self._write_json(404, {"error": "接口不存在"})

    def do_POST(self):
        path = self.path.split("?", 1)[0]
        action = POST_ROUTES.get(path)
        if action is None:
            self._write_json(404, {"error": "接口不存在"})
            return
        try:
            payload = self._read_json()
        except json.JSONDecodeError:
            self._write_json(400, {"error": "请求体必须是合法 JSON"})
            return
        try:
            result = getattr(self.service, action)(payload)
        except CertificationError as error:
            self._write_json(422, {"error": str(error)})
            return
        self._write_json(200, result)

    def _get_certificate_view(self, cert_id):
        try:
            self._write_json(200, self.service.public_certificate(cert_id))
        except CertificationError as error:
            self._write_json(404, {"error": str(error)})

    def _get_split_view(self, split_id):
        try:
            self._write_json(200, self.service.public_split(split_id))
        except CertificationError as error:
            self._write_json(404, {"error": str(error)})

    def _get_trace_view(self, cert_id):
        try:
            self._write_json(200, self.service.trace(cert_id))
        except CertificationError as error:
            self._write_json(404, {"error": str(error)})

    def _read_json(self):
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        return json.loads(raw.decode("utf-8") or "{}")

    def _write_json(self, status, payload):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args):
        return


def create_handler_class(service=None):
    """创建绑定独立服务实例的 Handler 子类，便于多实例部署与测试隔离。"""
    bound_service = service or build_service()

    class BoundHandler(Handler):
        service = bound_service

    BoundHandler.__name__ = "Handler"
    return BoundHandler


def main():
    parser = argparse.ArgumentParser(description=SERVICE_NAME)
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    if args.check:
        assert health_payload()["service"] == SERVICE_ID
        assert isinstance(build_service(), CertificationService)
        print("基础检查通过")
        return
    ThreadingHTTPServer(("0.0.0.0", args.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
