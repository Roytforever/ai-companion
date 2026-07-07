"""MCP 客户端 —— 连接外部 MCP 服务器，把其工具注册进本项目工具系统

参照 Hermes 的 MCP 集成思路：
- 配置来自 settings.MCP_SERVERS（stdio: command/args/env；http: url/headers）
- 启动时发现工具，注册进统一 tool_registry，命名前缀 mcp_<server>_<tool>
- 支持 notifications/tools/list_changed 动态重载（stdio）
- 尽量零硬依赖：优先复用官方 mcp SDK（若已安装），否则用内置最小 JSON-RPC 实现

内置最小实现满足 MCP 基础协议：
  initialize -> notifications/initialized -> tools/list -> tools/call
"""

import json
import logging
import os
import ssl
import subprocess
import threading
import time
import itertools
import concurrent.futures
import urllib.request
import urllib.error
import urllib.parse
from pathlib import Path

from tools.registry import registry, Tool
from config import settings

logger = logging.getLogger(__name__)

PROTOCOL_VERSION = "2024-11-05"


# ==============================
# 命名清洗 / 认证 / TLS 辅助（对齐 Hermes mcp-config-reference）
# ==============================

def _clean(name: str) -> str:
    """server/tool 名中的 - 和 . 清洗为 _（对齐 Hermes 工具命名规则）。"""
    return name.replace("-", "_").replace(".", "_")


def _expand(p: str) -> str:
    return os.path.expanduser(p) if p else p


def _build_ssl_context(cfg: dict):
    """依据 client_cert/client_key/ssl_verify 构造 SSLContext（mTLS）。

    ssl_verify: true(默认, 系统CA) / false(关闭校验, 不安全) / PEM 路径(自定义CA)。
    缺失的证书文件会在连接时快速失败。
    """
    client_cert = _expand(cfg.get("client_cert", ""))
    client_key = _expand(cfg.get("client_key", ""))
    ssl_verify = cfg.get("ssl_verify", True)
    if not client_cert and (ssl_verify is True or ssl_verify is None):
        return None  # 使用系统默认验证
    ctx = ssl.create_default_context()
    if ssl_verify is False:
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    elif isinstance(ssl_verify, str) and ssl_verify:
        ctx.load_verify_locations(_expand(ssl_verify))
    if client_cert:
        if not os.path.exists(client_cert):
            raise FileNotFoundError(f"MCP 客户端证书不存在：{client_cert}")
        key = _expand(client_key) if client_key else None
        if key and not os.path.exists(key):
            raise FileNotFoundError(f"MCP 客户端私钥不存在：{key}")
        # client_cert 可为 [cert, key] 或 [cert, key, password]
        if isinstance(client_cert, (list, tuple)):
            cert = client_cert
        else:
            cert = (client_cert, key) if key else client_cert
        ctx.load_cert_chain(*cert)
    return ctx


def _token_cache_path(server: str) -> Path:
    d = settings.MCP_TOKEN_DIR
    d.mkdir(parents=True, exist_ok=True)
    return d / f"{_clean(server)}.json"


def _load_cached_token(server: str):
    p = _token_cache_path(server)
    if p.exists():
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            if data.get("expires_at", 0) > time.time() + 30:
                return data.get("access_token")
        except Exception:
            pass
    return None


def _save_cached_token(server: str, token: str, expires_in: int = 3600):
    try:
        _token_cache_path(server).write_text(
            json.dumps({"access_token": token, "expires_at": time.time() + expires_in},
                       ensure_ascii=False),
            encoding="utf-8",
        )
    except Exception:
        pass


def _oauth_client_credentials(auth: dict) -> str:
    """OAuth 2.0 client_credentials 授权（headless 友好）。返回 bearer token。"""
    token_url = auth.get("token_url")
    if not token_url:
        raise ValueError("oauth client_credentials 需提供 token_url")
    body = {
        "grant_type": "client_credentials",
        "client_id": auth.get("client_id", ""),
        "client_secret": auth.get("client_secret", ""),
    }
    scope = auth.get("scope")
    if scope:
        body["scope"] = scope
    req = urllib.request.Request(
        token_url,
        data=urllib.parse.urlencode(body).encode("utf-8"),
        headers={"Content-Type": "application/x-www-form-urlencoded",
                 "Accept": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        tok = data.get("access_token")
        if not tok:
            raise ValueError(f"令牌端点未返回 access_token：{list(data.keys())}")
        _save_cached_token(auth.get("_server", "mcp"), tok, int(data.get("expires_in", 3600)))
        return tok
    except Exception as e:
        raise RuntimeError(f"OAuth client_credentials 失败：{e}")


def _resolve_auth_headers(server: str, cfg: dict) -> dict:
    """解析 auth 配置为请求头（含 Authorization）。

    支持：
    - auth: "oauth"  → 尝试读取缓存令牌（headless 不支持浏览器 PKCE，需手动放置或改用 client_credentials）
    - auth: {type:"bearer", token:"..."}  → 静态 bearer
    - auth: {type:"oauth", grant:"client_credentials", token_url, client_id, client_secret, scope}
                       → 走 client_credentials，令牌缓存复用
    - headers: {Authorization: "Bearer ..."}  → 静态头部（与 auth 合并）
    """
    headers: dict = {}
    if cfg.get("headers"):
        headers.update({k: str(v) for k, v in cfg["headers"].items()})

    auth = cfg.get("auth")
    if not auth:
        return headers

    if isinstance(auth, dict):
        atype = (auth.get("type") or "oauth").lower()
        if atype == "bearer":
            if auth.get("token"):
                headers["Authorization"] = f"Bearer {auth['token']}"
        elif atype in ("oauth", "oauth2"):
            grant = (auth.get("grant") or "client_credentials").lower()
            if grant == "client_credentials":
                auth = {**auth, "_server": server}
                cached = _load_cached_token(server)
                if cached:
                    headers["Authorization"] = f"Bearer {cached}"
                else:
                    headers["Authorization"] = f"Bearer {_oauth_client_credentials(auth)}"
            else:
                # PKCE 浏览器流程在 headless 下不支持；尝试缓存令牌
                cached = _load_cached_token(server)
                if cached:
                    headers["Authorization"] = f"Bearer {cached}"
                else:
                    logger.warning(
                        f"MCP {server}: 浏览器 PKCE OAuth 在 headless 下不可用，"
                        f"请改用语 auth:{{type:oauth,grant:client_credentials,...}} 或预置令牌缓存"
                    )
    elif isinstance(auth, str) and auth.strip().lower() == "oauth":
        cached = _load_cached_token(server)
        if cached:
            headers["Authorization"] = f"Bearer {cached}"
        else:
            logger.warning(
                f"MCP {server}: auth:oauth 需浏览器 PKCE，headless 不可用；"
                f"请改用 client_credentials 或预置 .mcp_tokens/{_clean(server)}.json"
            )
    return headers


# ==============================
# 传输层（JSON-RPC 2.0）
# ==============================

class _StdioTransport:
    """通过子进程 stdin/stdout 收发 JSON-RPC 行。"""

    def __init__(self, command: str, args: list, env: dict, timeout: float = 30):
        full_env = dict(__import__("os").environ)
        if env:
            full_env.update({k: str(v) for k, v in env.items()})
        self.proc = subprocess.Popen(
            [command, *list(args)],
            env=full_env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
            encoding="utf-8",
        )
        self._lock = threading.Lock()
        self._pending: dict = {}
        self._id_counter = itertools.count(1)
        self._notify_handlers: list = []
        self.timeout = timeout
        self._reader = threading.Thread(target=self._read_loop, daemon=True)
        self._reader.start()

    def _read_loop(self):
        try:
            for line in self.proc.stdout:
                line = line.strip()
                if not line:
                    continue
                try:
                    msg = json.loads(line)
                except Exception:
                    continue
                if "method" in msg and "id" not in msg:
                    for h in self._notify_handlers:
                        try:
                            h(msg)
                        except Exception:
                            pass
                elif "id" in msg:
                    fut = self._pending.pop(msg["id"], None)
                    if fut:
                        fut.set_result(msg)
        except Exception:
            pass

    def on_notification(self, handler):
        self._notify_handlers.append(handler)

    def request(self, method: str, params: dict | None = None, timeout: float | None = None):
        rid = next(self._id_counter)
        fut: concurrent.futures.Future = concurrent.futures.Future()
        self._pending[rid] = fut
        req = {"jsonrpc": "2.0", "id": rid, "method": method, "params": params or {}}
        with self._lock:
            self.proc.stdin.write(json.dumps(req) + "\n")
            self.proc.stdin.flush()
        try:
            resp = fut.result(timeout or self.timeout)
        except Exception as e:
            raise RuntimeError(f"MCP 请求 {method} 失败：{e}")
        if "error" in resp:
            raise RuntimeError(f"MCP 错误 {method}：{resp['error']}")
        return resp.get("result")

    def notify(self, method: str, params: dict | None = None):
        req = {"jsonrpc": "2.0", "method": method, "params": params or {}}
        with self._lock:
            self.proc.stdin.write(json.dumps(req) + "\n")
            self.proc.stdin.flush()

    def close(self):
        try:
            self.proc.stdin.close()
        except Exception:
            pass
        try:
            self.proc.terminate()
        except Exception:
            pass


class _HttpTransport:
    """通过 HTTP(S) POST 收发 JSON-RPC，支持 SSE 响应流。

    注意：MCP HTTP 服务器常为 localhost，故不使用 SSRF 安全 opener。
    """

    def __init__(self, url: str, headers: dict | None = None, timeout: float = 30,
                 ssl_context=None):
        self.url = url
        self.headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        }
        if headers:
            self.headers.update({k: str(v) for k, v in headers.items()})
        self.timeout = timeout
        self.ssl_context = ssl_context
        self._id_counter = itertools.count(1)
        self._session_id = None

    @staticmethod
    def _parse_sse(text: str) -> list[dict]:
        out = []
        for block in text.split("\n\n"):
            for line in block.splitlines():
                line = line.strip()
                if line.startswith("data:"):
                    payload = line[len("data:"):].strip()
                    if payload and payload != "[DONE]":
                        try:
                            out.append(json.loads(payload))
                        except Exception:
                            pass
        return out

    def request(self, method: str, params: dict | None = None, timeout: float | None = None):
        rid = next(self._id_counter)
        req = {"jsonrpc": "2.0", "id": rid, "method": method, "params": params or {}}
        data = json.dumps(req).encode("utf-8")
        hdrs = dict(self.headers)
        if self._session_id:
            hdrs["Mcp-Session-Id"] = self._session_id
        req_obj = urllib.request.Request(self.url, data=data, headers=hdrs, method="POST")
        try:
            with urllib.request.urlopen(req_obj, timeout=timeout or self.timeout,
                                        context=self.ssl_context) as resp:
                if "Mcp-Session-Id" in resp.headers:
                    self._session_id = resp.headers["Mcp-Session-Id"]
                raw = resp.read().decode("utf-8", errors="ignore")
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", errors="ignore")
            raise RuntimeError(f"MCP HTTP 错误 {e.code}：{body[:300]}")
        except Exception as e:
            raise RuntimeError(f"MCP HTTP 请求失败：{e}")

        # 解析：JSON 直回 或 SSE 流
        msgs = []
        try:
            msgs = [json.loads(raw)]
        except Exception:
            msgs = self._parse_sse(raw)
        for msg in msgs:
            if msg.get("id") == rid:
                if "error" in msg:
                    raise RuntimeError(f"MCP 错误 {method}：{msg['error']}")
                return msg.get("result")
        raise RuntimeError(f"MCP 未收到 id={rid} 的响应")

    def notify(self, method: str, params: dict | None = None):
        # 简单通知（fire-and-forget）
        rid = next(self._id_counter)
        req = {"jsonrpc": "2.0", "id": rid, "method": method, "params": params or {}}
        data = json.dumps(req).encode("utf-8")
        hdrs = dict(self.headers)
        if self._session_id:
            hdrs["Mcp-Session-Id"] = self._session_id
        try:
            req_obj = urllib.request.Request(self.url, data=data, headers=hdrs, method="POST")
            urllib.request.urlopen(req_obj, timeout=self.timeout, context=self.ssl_context).close()
        except Exception:
            pass

    def close(self):
        pass


# ==============================
# 单个 MCP 服务器连接
# ==============================

class MCPServer:
    """维护与一个 MCP 服务器的连接，并暴露工具发现/调用。"""

    def __init__(self, name: str, config: dict):
        self.name = name
        self.config = config
        self.transport = None
        self.tools: list[dict] = []
        self.resources: list[dict] = []
        self.prompts: list[dict] = []
        self.connected = False

    def connect(self, timeout: float = 30):
        cfg = self.config
        if cfg.get("url"):
            # 解析 auth（OAuth/mTLS）→ 请求头 + SSL 上下文
            headers = _resolve_auth_headers(self.name, cfg)
            ssl_ctx = _build_ssl_context(cfg)
            self.transport = _HttpTransport(
                cfg["url"], headers, timeout=float(cfg.get("timeout", timeout)),
                ssl_context=ssl_ctx,
            )
        elif cfg.get("command"):
            self.transport = _StdioTransport(
                cfg["command"], cfg.get("args", []), cfg.get("env"),
                timeout=float(cfg.get("timeout", timeout)),
            )
        else:
            raise ValueError(f"服务器 {self.name} 缺少 command 或 url")

        # 握手
        self.transport.request(
            "initialize",
            {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "ai-companion", "version": "1.0"},
            },
            timeout=timeout,
        )
        self.transport.notify("notifications/initialized")
        # 动态重载
        self.transport.on_notification(self._on_notification)
        self.connected = True
        self.list_tools(timeout=timeout)
        # 资源与提示（P0）：部分服务器不支持，失败则置空
        self.list_resources(timeout=timeout)
        self.list_prompts(timeout=timeout)

    def _on_notification(self, msg: dict):
        if msg.get("method") == "notifications/tools/list_changed":
            logger.info(f"MCP {self.name} 工具列表变更，下次调用将重新发现")
            self.tools = []  # 置空，触发惰性重载

    def list_tools(self, timeout: float = 30) -> list[dict]:
        if self.tools:
            return self.tools
        if not self.connected:
            self.connect(timeout=timeout)
        result = self.transport.request("tools/list", {}, timeout=timeout)
        self.tools = result.get("tools", []) if result else []
        return self.tools

    def call_tool(self, tool_name: str, arguments: dict, timeout: float = 30) -> str:
        if not self.tools:
            self.list_tools(timeout=timeout)
        result = self.transport.request(
            "tools/call", {"name": tool_name, "arguments": arguments or {}}, timeout=timeout
        )
        if not result:
            return ""
        parts = []
        for item in result.get("content", []):
            if item.get("type") == "text":
                parts.append(item.get("text", ""))
        return "\n".join(parts)

    # ---- 资源 / 提示（P0：MCP 不仅 tools） ----
    def list_resources(self, timeout: float = 30) -> list[dict]:
        if not self.connected:
            self.connect(timeout=timeout)
        try:
            res = self.transport.request("resources/list", {}, timeout=timeout)
            self.resources = res.get("resources", []) if res else []
        except Exception as e:
            self.resources = []
            logger.debug(f"MCP {self.name} resources/list 不支持：{e}")
        return self.resources

    def read_resource(self, uri: str, timeout: float = 30) -> str:
        if not self.connected:
            self.connect(timeout=timeout)
        try:
            res = self.transport.request("resources/read", {"uri": uri}, timeout=timeout)
            parts = []
            for item in (res or {}).get("contents", []):
                if isinstance(item, dict):
                    parts.append(item.get("text") or item.get("blob") or "")
                else:
                    parts.append(str(item))
            return "\n".join(parts)
        except Exception as e:
            return f"读取资源失败：{e}"

    def list_prompts(self, timeout: float = 30) -> list[dict]:
        if not self.connected:
            self.connect(timeout=timeout)
        try:
            res = self.transport.request("prompts/list", {}, timeout=timeout)
            self.prompts = res.get("prompts", []) if res else []
        except Exception as e:
            self.prompts = []
            logger.debug(f"MCP {self.name} prompts/list 不支持：{e}")
        return self.prompts

    def get_prompt(self, name: str, arguments: dict | None = None, timeout: float = 30) -> str:
        if not self.connected:
            self.connect(timeout=timeout)
        try:
            res = self.transport.request(
                "prompts/get", {"name": name, "arguments": arguments or {}}, timeout=timeout
            )
            parts = []
            for item in (res or {}).get("messages", []):
                c = item.get("content")
                if isinstance(c, dict):
                    parts.append(c.get("text") or "")
                else:
                    parts.append(str(c))
            return "\n".join(parts)
        except Exception as e:
            return f"获取提示失败：{e}"

    def close(self):
        if self.transport:
            self.transport.close()
        self.connected = False


# ==============================
# 管理器：连接所有服务器 + 注册工具
# ==============================

class MCPManager:
    """加载配置、连接所有启用的 MCP 服务器，并把工具注册进项目 tool_registry。"""

    def __init__(self):
        self.servers: dict[str, MCPServer] = {}
        self._registered: dict[str, list[str]] = {}  # server -> [tool names]

    def connect_all(self, timeout: float = 30) -> dict[str, list[str]]:
        """连接所有 enabled 服务器并注册工具。返回 {server: [tool_names]}。"""
        self._registered = {}
        for name, cfg in (settings.MCP_SERVERS or {}).items():
            if cfg.get("enabled") is False:
                continue
            try:
                srv = MCPServer(name, cfg)
                srv.connect(timeout=timeout)
                names = self._register_tools(srv)
                self.servers[name] = srv
                self._registered[name] = names
                logger.info(f"MCP 服务器 {name} 已连接，注册工具：{names}")
            except Exception as e:
                logger.warning(f"MCP 服务器 {name} 连接失败：{e}")
        return self._registered

    def _register_tools(self, srv: MCPServer) -> list[str]:
        names = []
        tools_cfg = srv.config.get("tools") or {}
        include = tools_cfg.get("include")
        exclude = tools_cfg.get("exclude")
        if isinstance(include, str):
            include = [include]
        if isinstance(exclude, str):
            exclude = [exclude]
        # include 优先；两者都设时 include 胜出（对齐 Hermes）
        for t in srv.tools:
            raw_name = t.get("name", "")
            # 工具过滤（用原始 MCP 工具名匹配，含连字符/点）
            if include:
                if raw_name not in include:
                    continue
            elif exclude:
                if raw_name in exclude:
                    continue
            full_name = f"mcp_{_clean(srv.name)}_{_clean(raw_name)}"
            schema = t.get("inputSchema", {}) or {}
            props = schema.get("properties", {}) or {}
            required = schema.get("required", []) or []
            # MCP 的 inputSchema 已是 JSON Schema 类型，直接复用（无需 type_map 转换）
            parameters = {}
            for pname, pinfo in props.items():
                ptype = pinfo.get("type", "string")
                if isinstance(ptype, list):
                    ptype = ptype[0] if ptype else "string"
                parameters[pname] = {
                    "type": ptype if isinstance(ptype, str) else "string",
                    "description": pinfo.get("description", f"参数 {pname}"),
                }
                if pname in required:
                    parameters[pname]["required"] = True
            # 闭包捕获工具名（默认参数避免 late binding）
            srv_ref = srv

            def _make_call(tool_name: str):
                def _call(**kwargs):
                    return srv_ref.call_tool(tool_name, kwargs)
                return _call

            fn = _make_call(raw_name)
            fn.__name__ = full_name
            tool = Tool(
                name=full_name,
                description=f"[MCP:{srv.name}] {t.get('description', raw_name)}",
                fn=fn,
                parameters=parameters,
            )
            registry._tools[full_name] = tool
            names.append(full_name)

        # 资源 / 提示访问工具（P0：MCP 不仅 tools）
        srv_ref = srv
        base = _clean(srv.name)

        def _make_res_list():
            def _call():
                rs = srv_ref.list_resources()
                if not rs:
                    return "(无可用资源)"
                return "\n".join(
                    f"- {r.get('uri')}: {r.get('name', '')} {r.get('description', '')}"
                    for r in rs
                )
            return _call

        def _make_res_read():
            def _call(uri: str):
                return srv_ref.read_resource(uri)
            return _call

        def _make_prompt_list():
            def _call():
                ps = srv_ref.list_prompts()
                if not ps:
                    return "(无可用提示)"
                return "\n".join(
                    f"- {p.get('name')}: {p.get('description', '')}" for p in ps
                )
            return _call

        def _make_prompt_get():
            def _call(name: str, arguments: str = "{}"):
                try:
                    a = json.loads(arguments) if arguments else {}
                except Exception:
                    a = {}
                return srv_ref.get_prompt(name, a)
            return _call

        rp_tools = [
            (f"mcp_{base}_list_resources", f"[MCP:{srv.name}] 列出服务器提供的资源",
             _make_res_list(), {}),
            (f"mcp_{base}_read_resource", f"[MCP:{srv.name}] 读取指定资源的内容",
             _make_res_read(), {"uri": {"type": "string", "description": "资源 URI", "required": True}}),
            (f"mcp_{base}_list_prompts", f"[MCP:{srv.name}] 列出服务器提供的提示模板",
             _make_prompt_list(), {}),
            (f"mcp_{base}_get_prompt", f"[MCP:{srv.name}] 用参数实例化某个提示模板",
             _make_prompt_get(),
             {"name": {"type": "string", "description": "提示名", "required": True},
              "arguments": {"type": "string", "description": "JSON 参数字符串"}}),
        ]
        for tname, tdesc, fn, params in rp_tools:
            fn.__name__ = tname
            tool = Tool(name=tname, description=tdesc, fn=fn, parameters=params)
            registry._tools[tname] = tool
            names.append(tname)
        return names

    def status(self) -> list[dict]:
        """返回各服务器连接状态摘要（供 UI 展示）。"""
        out = []
        for name, srv in self.servers.items():
            out.append({
                "name": name,
                "connected": srv.connected,
                "tools": [t.get("name") for t in srv.tools],
            })
        return out

    def close_all(self):
        for srv in self.servers.values():
            try:
                srv.close()
            except Exception:
                pass
        self.servers = {}
