#!/usr/bin/python3
"""Native GTK/WebKitGTK window for the local receipt service.

The launcher owns the service lifecycle. This module only displays its local
workspace and integrates file selection, clipboard images, and downloads.
"""
from __future__ import annotations

import argparse
import base64
from datetime import datetime
import json
from pathlib import Path
import re
import sys
import threading
import time
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

if __package__:
    from .drop_import import DropImportError, MAX_FILE_BYTES, resolve_drop_payload
else:
    from drop_import import DropImportError, MAX_FILE_BYTES, resolve_drop_payload

try:
    import gi

    gi.require_version("Gtk", "3.0")
    gi.require_version("Gdk", "3.0")
    try:
        gi.require_version("WebKit2", "4.1")
        WEBKIT_API_VERSION = "4.1"
    except ValueError:
        gi.require_version("WebKit2", "4.0")
        WEBKIT_API_VERSION = "4.0"
    from gi.repository import Gdk, Gio, GLib, Gtk, WebKit2
except (ImportError, ValueError) as exc:
    print("无法启动凭证归档：请安装 python3-gi、gir1.2-gtk-3.0，以及 gir1.2-webkit2-4.1 或 gir1.2-webkit2-4.0。", file=sys.stderr)
    print(str(exc), file=sys.stderr)
    raise SystemExit(1) from exc


WEBKIT_VERSION = (WebKit2.get_major_version(), WebKit2.get_minor_version(), WebKit2.get_micro_version())
if WEBKIT_VERSION < (2, 36, 0):
    print("无法启动凭证归档：请通过系统更新安装 WebKitGTK 2.36 或更新版本。", file=sys.stderr)
    raise SystemExit(1)

APP_NAME = "凭证归档"
APP_ID = "org.receiptarchive.Desktop"
LAST_BATCH_KEY = "receipt-studio:last-batch"
# Keep file lists first: a file manager can offer an image preview for only the
# first item of a multi-file selection. An unreadable URI falls back to bytes.
DROP_TARGETS = (
    "text/uri-list", "application/vnd.portal.filetransfer",
    "x-special/gnome-copied-files", "image/png", "image/jpeg", "image/webp",
    "image/bmp", "image/tiff", "image/x-bmp", "image/x-ms-bmp",
    "application/x-qt-image", "text/html", "text/plain;charset=utf-8",
    "text/plain", "UTF8_STRING", "text/x-moz-url", "STRING", "TEXT",
)
DROP_FALLBACK_MESSAGE = "没有收到可读取的图片，请在微信中打开原图后再拖入，或复制图片后按 Ctrl+V。"


def local_url(value: str) -> str:
    """Accept only the loopback service supplied by the trusted launcher."""
    try:
        parsed = urlsplit(value)
        valid = (parsed.scheme == "http" and parsed.hostname in {"localhost", "127.0.0.1", "::1"}
                 and not parsed.username and not parsed.password and 1 <= (parsed.port or 80) <= 65535)
    except ValueError:
        valid = False
    if not valid:
        raise argparse.ArgumentTypeError("服务地址必须是本机 http://127.0.0.1:端口 或 localhost 地址。")
    return value


def batch_id(value: str) -> str:
    if not re.fullmatch(r"[a-f0-9]{32}", value):
        raise argparse.ArgumentTypeError("批次编号应为 32 位小写十六进制字符。")
    return value


def origin(value: str) -> tuple[str, str | None, int | None]:
    try:
        parsed = urlsplit(value)
        return parsed.scheme, parsed.hostname, parsed.port or (80 if parsed.scheme == "http" else 443)
    except ValueError:
        return "", None, None


def safe_filename(suggested: str | None) -> str:
    value = (suggested or "凭证归档.zip").replace("\\", "/").rsplit("/", 1)[-1]
    value = "".join(char for char in value if ord(char) >= 32 and char != "\x7f").strip(" .")
    if not value:
        value = "凭证归档.zip"
    if len(value.encode("utf-8")) > 220:
        suffix = Path(value).suffix[:12]
        stem = value[:-len(suffix)] if suffix else value
        value = stem.encode("utf-8")[:190].decode("utf-8", "ignore") + suffix
    return value


class ReceiptApplication(Gtk.Application):
    def __init__(self, url: str, resume_batch: str | None = None):
        super().__init__(application_id=APP_ID, flags=Gio.ApplicationFlags.FLAGS_NONE)
        self.url = url
        self.allowed_origin = origin(url)
        self.resume_batch = resume_batch
        self.window = None
        self.webview = None
        self.web_context = None
        self.stack = None
        self.notice_bar = None
        self.notice_label = None
        self.notice_revealer = None
        self.notice_timeout = None
        self.downloads: dict[object, dict] = {}
        self.dialogs: set = set()
        self.load_failed = False
        self.drop_transfers: dict[object, dict] = {}
        self.completed_drop_contexts: list = []
        self.drop_hover_context = None
        self.closing = False
        self.load_generation = 0
        self.drop_script_serial = 0
        self.download_directory = Path(GLib.get_user_special_dir(GLib.UserDirectory.DIRECTORY_DOWNLOAD) or (Path.home() / "Downloads"))

    def do_activate(self):
        if self.window:
            self.window.present()
            return
        self._build_window()
        self.window.show_all()
        self.notice_revealer.set_reveal_child(False)
        self._load_workspace()

    def _build_window(self):
        self.window = Gtk.ApplicationWindow(application=self, title=APP_NAME)
        self.window.set_wmclass("receipt-archive", "ReceiptArchive")
        self.window.set_default_size(1180, 850)
        self.window.set_size_request(660, 540)
        icon_path = Path(__file__).with_name("icon.svg")
        if icon_path.exists():
            self.window.set_icon_from_file(str(icon_path))
        self.window.connect("key-press-event", self._on_key_press)
        self.window.connect("delete-event", self._on_close)

        header = Gtk.HeaderBar(title=APP_NAME, subtitle="本机工作区", show_close_button=True)
        self.window.set_titlebar(header)
        refresh = Gtk.Button.new_from_icon_name("view-refresh-symbolic", Gtk.IconSize.BUTTON)
        refresh.set_tooltip_text("刷新工作区（Ctrl+R）")
        refresh.connect("clicked", lambda *_: self._reload())
        header.pack_start(refresh)
        folder = Gtk.Button.new_from_icon_name("folder-download-symbolic", Gtk.IconSize.BUTTON)
        folder.set_tooltip_text("打开下载文件夹")
        folder.connect("clicked", self._open_download_folder)
        header.pack_end(folder)

        data_dir = Path(GLib.get_user_data_dir()) / "receipt-archive" / "webview"
        cache_dir = Path(GLib.get_user_cache_dir()) / "receipt-archive"
        for directory in (data_dir, cache_dir):
            directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.website_data_manager = WebKit2.WebsiteDataManager(
            base_data_directory=str(data_dir), base_cache_directory=str(cache_dir))
        # This WebView talks only to the local service; external links use Gio.
        self.website_data_manager.set_network_proxy_settings(WebKit2.NetworkProxyMode.NO_PROXY, None)
        self.web_context = WebKit2.WebContext.new_with_website_data_manager(self.website_data_manager)
        self.web_context.connect("download-started", self._download_started)
        content = WebKit2.UserContentManager()
        # Older GI bindings reject None for the named-world async API. Run in
        # the default page world and report only the resolved import result.
        if not hasattr(WebKit2.WebView, "call_async_javascript_function"):
            content.register_script_message_handler("receiptDropResult")
            content.connect("script-message-received::receiptDropResult", self._legacy_drop_result)
        native_drop_script = """if (location.origin === %s) {
            window.receiptNativeDrop = true;
        }""" % json.dumps(self._origin_url())
        content.add_script(WebKit2.UserScript.new(
            native_drop_script, WebKit2.UserContentInjectedFrames.TOP_FRAME,
            WebKit2.UserScriptInjectionTime.START, None, None))
        if self.resume_batch:
            # The batch ID is validated before being serialized into this script.
            # Respect the WebView's current batch if it already has one.
            script = """(() => { try {
                if (location.origin === %s && !localStorage.getItem(%s)) {
                    localStorage.setItem(%s, %s);
                }
            } catch (_) {} })();""" % (
                json.dumps(self._origin_url()), json.dumps(LAST_BATCH_KEY),
                json.dumps(LAST_BATCH_KEY), json.dumps(self.resume_batch))
            content.add_script(WebKit2.UserScript.new(
                script, WebKit2.UserContentInjectedFrames.TOP_FRAME,
                WebKit2.UserScriptInjectionTime.START, None, None))
        self.webview = WebKit2.WebView(web_context=self.web_context, user_content_manager=content)
        settings = self.webview.get_settings()
        settings.set_enable_html5_local_storage(True)
        settings.set_enable_developer_extras(False)
        settings.set_allow_file_access_from_file_urls(False)
        settings.set_enable_media_stream(False)
        self.webview.connect("load-changed", self._load_changed)
        self.webview.connect("load-failed", self._load_failed)
        self.webview.connect("decide-policy", self._decide_policy)
        self.webview.connect("run-file-chooser", self._choose_upload_files)
        self.webview.connect("permission-request", self._deny_permission)
        self.webview.connect("web-process-terminated", self._web_process_terminated)
        self._configure_native_drops()

        root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.stack = Gtk.Stack()
        self.stack.add_named(self.webview, "workspace")
        error_panel = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18)
        error_panel.set_halign(Gtk.Align.CENTER)
        error_panel.set_valign(Gtk.Align.CENTER)
        error_panel.set_border_width(35)
        error_icon = Gtk.Image.new_from_icon_name("network-offline-symbolic", Gtk.IconSize.DIALOG)
        error_panel.pack_start(error_icon, False, False, 0)
        error_title = Gtk.Label(label="暂时无法打开凭证归档")
        error_title.get_style_context().add_class("title")
        error_panel.pack_start(error_title, False, False, 0)
        self.error_label = Gtk.Label(label="请点击重试。如果仍无法打开，请关闭并重新启动凭证归档。\n保存在本机的截图与配对不会因此删除。")
        self.error_label.set_justify(Gtk.Justification.CENTER)
        self.error_label.set_line_wrap(True)
        error_panel.pack_start(self.error_label, False, False, 0)
        retry = Gtk.Button(label="重新连接")
        retry.set_halign(Gtk.Align.CENTER)
        retry.connect("clicked", lambda *_: self._reload())
        error_panel.pack_start(retry, False, False, 0)
        self.stack.add_named(error_panel, "error")
        root.pack_start(self.stack, True, True, 0)
        self.notice_revealer = Gtk.Revealer()
        self.notice_bar = Gtk.InfoBar(show_close_button=True)
        self.notice_bar.connect("response", lambda *_: self.notice_revealer.set_reveal_child(False))
        self.notice_label = Gtk.Label(xalign=0)
        self.notice_label.set_line_wrap(True)
        self.notice_label.set_selectable(True)
        self.notice_bar.get_content_area().add(self.notice_label)
        self.notice_revealer.add(self.notice_bar)
        root.pack_end(self.notice_revealer, False, False, 0)
        self.window.add(root)
        self.stack.set_visible_child_name("workspace")

    def _origin_url(self):
        scheme, host, port = self.allowed_origin
        host = f"[{host}]" if ":" in (host or "") else host
        return f"{scheme}://{host}" + (f":{port}" if port != 80 else "")

    def _notice(self, message: str, error: bool = False):
        if self.notice_timeout:
            GLib.source_remove(self.notice_timeout)
        self.notice_bar.set_message_type(Gtk.MessageType.ERROR if error else Gtk.MessageType.INFO)
        self.notice_label.set_text(message)
        self.notice_revealer.set_reveal_child(True)
        self.notice_timeout = GLib.timeout_add_seconds(12 if error else 7, self._hide_notice)

    def _hide_notice(self):
        self.notice_timeout = None
        self.notice_revealer.set_reveal_child(False)
        return GLib.SOURCE_REMOVE

    def _reload(self):
        self.load_failed = False
        self.stack.set_visible_child_name("workspace")
        self._load_workspace()

    def _load_workspace(self):
        # An upgraded native package may otherwise reuse a previously cached
        # index.html that points at an older script. Only discard HTTP caches;
        # localStorage, cookies, screenshots, and backend batches are untouched.
        self.load_generation += 1
        generation = self.load_generation
        parsed = urlsplit(self.url)
        query = [(key, value) for key, value in parse_qsl(parsed.query, keep_blank_values=True)
                 if key != "_receipt_desktop"]
        query.append(("_receipt_desktop", str(time.time_ns())))
        document_url = urlunsplit(parsed._replace(query=urlencode(query)))

        def cleared(manager, result, _data):
            try:
                manager.clear_finish(result)
            except GLib.Error:
                # The document's fresh cache key remains a fallback if an old
                # WebKit cache entry cannot be removed immediately.
                self.web_context.clear_cache()
            if not self.closing and generation == self.load_generation:
                self.webview.load_uri(document_url)
        self.website_data_manager.clear(
            WebKit2.WebsiteDataTypes.MEMORY_CACHE | WebKit2.WebsiteDataTypes.DISK_CACHE,
            0, None, cleared, None)

    def _load_changed(self, _view, event):
        if event == WebKit2.LoadEvent.STARTED:
            self.load_failed = False
        elif event == WebKit2.LoadEvent.FINISHED and not self.load_failed:
            self.stack.set_visible_child_name("workspace")
            for context, transfer in list(self.drop_transfers.items()):
                if transfer.get("awaiting_bridge") and not transfer.get("bridge_probe"):
                    self._wait_for_native_drop_bridge(context)

    def _load_failed(self, _view, _event, _uri, error):
        if error.matches(WebKit2.network_error_quark(), WebKit2.NetworkError.CANCELLED):
            return True
        self.load_failed = True
        self.stack.set_visible_child_name("error")
        return True

    def _web_process_terminated(self, _view, _reason):
        self.load_failed = True
        self.error_label.set_text("工作区意外停止，请点击重新连接。\n保存在本机的截图与配对不会因此删除。")
        self.stack.set_visible_child_name("error")

    def _same_origin(self, uri: str) -> bool:
        if uri.startswith("blob:"):
            uri = uri[5:]
        return origin(uri) == self.allowed_origin

    def _decide_policy(self, _view, decision, decision_type):
        if decision_type not in (WebKit2.PolicyDecisionType.NAVIGATION_ACTION,
                                 WebKit2.PolicyDecisionType.NEW_WINDOW_ACTION):
            return False
        action = decision.get_navigation_action()
        uri = action.get_request().get_uri() or ""
        if self._same_origin(uri):
            if decision_type == WebKit2.PolicyDecisionType.NEW_WINDOW_ACTION:
                decision.ignore()
                self.webview.load_uri(uri)
            else:
                decision.use()
            return True
        decision.ignore()
        parsed = urlsplit(uri)
        clicked_link = action.is_user_gesture() and action.get_navigation_type() == WebKit2.NavigationType.LINK_CLICKED
        if clicked_link and parsed.scheme in {"https", "http", "mailto"}:
            try:
                Gio.AppInfo.launch_default_for_uri(uri, self.window.get_display().get_app_launch_context())
            except GLib.Error:
                self._notice("无法打开这个链接，请检查系统的默认浏览器设置。", True)
        return True

    @staticmethod
    def _deny_permission(_view, request):
        request.deny()
        return True

    def _choose_upload_files(self, _view, request):
        chooser = Gtk.FileChooserNative.new("添加订单与支付截图", self.window,
            Gtk.FileChooserAction.OPEN, "添加截图", "取消")
        chooser.set_select_multiple(request.get_select_multiple())
        images = Gtk.FileFilter()
        images.set_name("截图图片（PNG、JPG、WEBP、TIFF、BMP）")
        for mime in ("image/png", "image/jpeg", "image/webp", "image/tiff", "image/bmp"):
            images.add_mime_type(mime)
        for pattern in ("*.png", "*.PNG", "*.jpg", "*.JPG", "*.jpeg", "*.JPEG", "*.webp", "*.WEBP", "*.tif", "*.tiff", "*.bmp"):
            images.add_pattern(pattern)
        chooser.add_filter(images)
        self.dialogs.add(chooser)

        def response(dialog, code):
            try:
                paths = dialog.get_filenames() if code == Gtk.ResponseType.ACCEPT else []
                request.select_files(paths) if paths else request.cancel()
            finally:
                self.dialogs.discard(dialog)
                dialog.destroy()
        chooser.connect("response", response)
        chooser.show()
        return True

    def _configure_native_drops(self):
        # WebKit already owns a GTK drop destination with custom default
        # behavior. Extend its formats without enabling GTK's automatic data
        # requests/finish, which would duplicate our explicit transfer.
        targets = self.webview.drag_dest_get_target_list()
        if targets is None:
            entries = [Gtk.TargetEntry.new(name, Gtk.TargetFlags(0), 1000 + index)
                       for index, name in enumerate(DROP_TARGETS)]
            self.webview.drag_dest_set(Gtk.DestDefaults(0), entries, Gdk.DragAction.COPY)
        else:
            for index, name in enumerate(DROP_TARGETS):
                atom = Gdk.Atom.intern(name, False)
                if not targets.find(atom)[0]:
                    targets.add(atom, Gtk.TargetFlags(0), 1000 + index)
        self.webview.connect("drag-motion", self._native_drag_motion)
        self.webview.connect("drag-leave", self._native_drag_leave)
        self.webview.connect("drag-drop", self._native_drag_drop)
        self.webview.connect("drag-data-received", self._native_drag_data_received)

    @staticmethod
    def _offered_drop_targets(context):
        offered = {atom.name().lower(): atom for atom in context.list_targets()}
        result = []
        for name in DROP_TARGETS:
            atom = offered.get(name.lower())
            if atom is not None:
                result.append((name, atom))
        # MIME charset spelling differs between Qt versions. Keep those atoms
        # intact for the source, while normalizing only for the parser later.
        known = {name.lower() for name, _atom in result}
        for name, atom in offered.items():
            base = name.split(";", 1)[0].strip()
            if name not in known and base in {"text/plain", "text/html"}:
                result.append((name, atom))
        return result

    def _native_drag_motion(self, widget, context, _x, _y, timestamp):
        if not self._offered_drop_targets(context):
            return False
        Gdk.drag_status(context, Gdk.DragAction.COPY, timestamp)
        if self.drop_hover_context != context:
            self.drop_hover_context = context
            widget.drag_highlight()
        return True

    def _native_drag_leave(self, widget, context, _timestamp):
        if self.drop_hover_context == context:
            self.drop_hover_context = None
            widget.drag_unhighlight()
            widget.stop_emission_by_name("drag-leave")
        elif context in self.drop_transfers or context in self.completed_drop_contexts:
            # We suppressed WebKit's corresponding drag-motion; it must not
            # receive a leave for a transfer it never started.
            widget.stop_emission_by_name("drag-leave")

    def _native_drag_drop(self, widget, context, _x, _y, timestamp):
        targets = self._offered_drop_targets(context)
        if not targets:
            return False
        widget.drag_unhighlight()
        self.drop_hover_context = None
        if context in self.drop_transfers:
            return True
        self.drop_transfers[context] = {
            "widget": widget, "context": context, "time": timestamp,
            "targets": targets, "index": 0, "requested": None,
            "timer": None, "error": DROP_FALLBACK_MESSAGE,
            "files": None, "bridge_reloads": 0, "awaiting_bridge": False,
            "bridge_probe": None,
        }
        self._request_next_drop_target(context)
        return True

    def _request_next_drop_target(self, context):
        transfer = self.drop_transfers.get(context)
        if transfer is None:
            return
        if transfer["timer"]:
            GLib.source_remove(transfer["timer"])
            transfer["timer"] = None
        if transfer["index"] >= len(transfer["targets"]):
            self._notice(transfer["error"], True)
            self._finish_native_drop(context, False)
            return
        name, atom = transfer["targets"][transfer["index"]]
        transfer["index"] += 1
        transfer["requested"] = name
        transfer["timer"] = GLib.timeout_add_seconds(8, self._drop_target_timeout, context, name)
        try:
            transfer["widget"].drag_get_data(context, atom, transfer["time"])
        except (GLib.Error, TypeError, RuntimeError):
            self._request_next_drop_target(context)

    def _drop_target_timeout(self, context, requested):
        transfer = self.drop_transfers.get(context)
        if transfer and transfer["requested"] == requested:
            transfer["timer"] = None
            self._request_next_drop_target(context)
        return GLib.SOURCE_REMOVE

    def _native_drag_data_received(self, widget, context, _x, _y, selection, _info, _timestamp):
        transfer = self.drop_transfers.get(context)
        if transfer is None:
            if context in self.completed_drop_contexts:
                widget.stop_emission_by_name("drag-data-received")
            return
        # Only suppress WebKit for a drop this handler claimed. Never process
        # hover probes or an unrelated default WebKit data request.
        widget.stop_emission_by_name("drag-data-received")
        requested = transfer["requested"]
        if not requested or selection.get_target().name().lower() != requested.lower():
            return
        if transfer["timer"]:
            GLib.source_remove(transfer["timer"])
            transfer["timer"] = None
        transfer["requested"] = None
        try:
            mime = requested.split(";", 1)[0].strip().lower()
            if mime in {"text/uri-list", "application/vnd.portal.filetransfer"}:
                # GTK 3.24.37+ resolves sandbox FileTransfer portal offers here.
                uris = selection.get_uris()
                if uris:
                    data = "\r\n".join(uris).encode("utf-8")
                    mime = "text/uri-list"
                else:
                    data = bytes(selection.get_data() or b"")
            else:
                data = bytes(selection.get_data() or b"")
            if mime == "application/x-qt-image":
                pixbuf = selection.get_pixbuf()
                if pixbuf is not None:
                    encoded, image = pixbuf.save_to_bufferv("png", [], [])
                    if encoded:
                        data, mime = bytes(image), "image/png"
            if len(data) > MAX_FILE_BYTES and mime.startswith("image/"):
                raise DropImportError("图片超过 20 MB，请压缩后再导入。")
        except DropImportError as exc:
            transfer["error"] = str(exc)
            self._request_next_drop_target(context)
            return
        except (GLib.Error, TypeError, ValueError, UnicodeError):
            self._request_next_drop_target(context)
            return

        # Reading a file list and encoding images can take time. Keep GTK
        # responsive; this worker touches only copied bytes and local files.
        def resolve():
            try:
                files = resolve_drop_payload(mime, data)
                error = None
            except DropImportError as exc:
                files, error = None, str(exc)
            except Exception:
                files, error = None, DROP_FALLBACK_MESSAGE
            GLib.idle_add(self._resolved_native_drop, context, files, error)
        threading.Thread(target=resolve, name="receipt-native-drop", daemon=True).start()

    def _resolved_native_drop(self, context, files, error):
        transfer = self.drop_transfers.get(context)
        if transfer is None or self.closing:
            return GLib.SOURCE_REMOVE
        if error or not files:
            transfer["error"] = error or DROP_FALLBACK_MESSAGE
            self._request_next_drop_target(context)
            return GLib.SOURCE_REMOVE
        if not self._same_origin(self.webview.get_uri() or ""):
            self._finish_native_drop(context, False)
            return GLib.SOURCE_REMOVE
        transfer["files"] = files
        self._dispatch_native_drop(context)
        return GLib.SOURCE_REMOVE

    def _dispatch_native_drop(self, context):
        transfer = self.drop_transfers.get(context)
        if transfer is None:
            return
        if transfer["timer"]:
            GLib.source_remove(transfer["timer"])
        script = """if (typeof window.receiptNativeImport !== 'function' ||
                typeof window.receiptWorkspaceReady === 'undefined') return 'unavailable';
            if (!window.receiptWorkspaceReady) return 'initializing';
            return await window.receiptNativeImport(%s);""" % json.dumps(transfer["files"], ensure_ascii=True)
        transfer["timer"] = GLib.timeout_add_seconds(20, self._drop_import_timeout, context)
        if hasattr(self.webview, "call_async_javascript_function"):
            self.webview.call_async_javascript_function(
                script, -1, None, None, None, None, self._native_drop_imported, context)
        else:
            self.drop_script_serial += 1
            transfer["script_token"] = self.drop_script_serial
            source = """(async () => { %s })().then(
                status => window.webkit.messageHandlers.receiptDropResult.postMessage(
                    JSON.stringify({id: %d, status: status})),
                () => window.webkit.messageHandlers.receiptDropResult.postMessage(
                    JSON.stringify({id: %d, status: 'error'})));
                undefined;""" % (script, self.drop_script_serial, self.drop_script_serial)
            self._evaluate_javascript(source, self._legacy_drop_dispatched, context)

    def _evaluate_javascript(self, script, callback, data=None):
        if hasattr(self.webview, "evaluate_javascript"):
            self.webview.evaluate_javascript(script, -1, None, None, None, callback, data)
        else:
            self.webview.run_javascript(script, None, callback, data)

    @staticmethod
    def _javascript_result(view, result, asynchronous=False):
        if asynchronous:
            return view.call_async_javascript_function_finish(result)
        if not asynchronous and hasattr(view, "evaluate_javascript_finish"):
            return view.evaluate_javascript_finish(result)
        return view.run_javascript_finish(result).get_js_value()

    def _legacy_drop_dispatched(self, view, result, context):
        try:
            self._javascript_result(view, result)
        except GLib.Error:
            self._native_drop_reply(context, "error")

    def _legacy_drop_result(self, _content, result):
        try:
            message = json.loads(result.get_js_value().to_string())
        except (ValueError, GLib.Error):
            return
        if not isinstance(message, dict) or not isinstance(message.get("id"), int) or not isinstance(message.get("status"), str):
            return
        for context, transfer in list(self.drop_transfers.items()):
            if transfer.get("script_token") == message["id"]:
                self._native_drop_reply(context, message["status"])
                return

    def _native_drop_imported(self, view, result, context):
        if context not in self.drop_transfers:
            return
        try:
            status = self._javascript_result(view, result, asynchronous=True).to_string()
        except GLib.Error:
            status = "error"
        self._native_drop_reply(context, status)

    def _native_drop_reply(self, context, status):
        if context not in self.drop_transfers:
            return
        if status in {"unavailable", "initializing"}:
            transfer = self.drop_transfers[context]
            if transfer["timer"]:
                GLib.source_remove(transfer["timer"])
            transfer["timer"] = GLib.timeout_add_seconds(25, self._drop_import_timeout, context)
            transfer["awaiting_bridge"] = True
            if status == "unavailable" and transfer["bridge_reloads"] == 0:
                transfer["bridge_reloads"] += 1
                self._notice("正在载入最新工作区，拖入的图片会保留并自动继续导入。")
                self._reload()
            else:
                self._wait_for_native_drop_bridge(context)
            return
        success = status == "accepted"
        if not success:
            messages = {
                "processing": "正在识别截图，请识别完成后再拖入图片。",
                "dialog-open": "请先关闭当前对话框，再拖入图片。",
                "unavailable": "工作区尚未准备好，请等待界面载入后重试。",
            }
            self._notice(messages.get(status, "图片尚未导入，请打开原图后重试，或使用「选择文件」导入。"), True)
        self._finish_native_drop(context, success)

    def _wait_for_native_drop_bridge(self, context):
        transfer = self.drop_transfers.get(context)
        if transfer is None or not transfer["awaiting_bridge"] or self.closing:
            return GLib.SOURCE_REMOVE
        transfer["bridge_probe"] = None
        self._evaluate_javascript(
            "document.readyState === 'complete' && typeof window.receiptNativeImport === 'function' && window.receiptWorkspaceReady === true",
            self._native_drop_bridge_ready, context)
        return GLib.SOURCE_REMOVE

    def _native_drop_bridge_ready(self, view, result, context):
        transfer = self.drop_transfers.get(context)
        if transfer is None or not transfer["awaiting_bridge"]:
            return
        try:
            ready = self._javascript_result(view, result).to_boolean()
        except GLib.Error:
            ready = False
        if ready and self._same_origin(view.get_uri() or ""):
            transfer["awaiting_bridge"] = False
            self._dispatch_native_drop(context)
        elif not transfer["bridge_probe"]:
            transfer["bridge_probe"] = GLib.timeout_add(250, self._wait_for_native_drop_bridge, context)

    def _drop_import_timeout(self, context):
        transfer = self.drop_transfers.get(context)
        if transfer:
            transfer["timer"] = None
            self._notice("图片导入未完成，请等待工作区载入后重试。", True)
            self._finish_native_drop(context, False)
        return GLib.SOURCE_REMOVE

    def _finish_native_drop(self, context, success):
        transfer = self.drop_transfers.pop(context, None)
        if transfer is None:
            return
        self.completed_drop_contexts.append(context)
        del self.completed_drop_contexts[:-32]
        if transfer["timer"]:
            GLib.source_remove(transfer["timer"])
        if transfer.get("bridge_probe"):
            GLib.source_remove(transfer["bridge_probe"])
        # COPY only: never ask a chat client/file manager to delete its source.
        Gtk.drag_finish(context, success, False, transfer["time"])

    def _download_started(self, _context, download):
        if download.get_web_view() != self.webview:
            return
        self.downloads[download] = {"failed": False, "cancelled": False, "path": None, "chooser": None}
        download.connect("decide-destination", self._download_destination)
        download.connect("failed", self._download_failed)
        download.connect("finished", self._download_finished)

    def _download_destination(self, download, suggested_filename):
        info = self.downloads.get(download)
        if info is None:
            download.cancel()
            return True
        chooser = Gtk.FileChooserNative.new("保存报销文档", self.window,
            Gtk.FileChooserAction.SAVE, "保存文档", "取消")
        chooser.set_do_overwrite_confirmation(True)
        chooser.set_current_name(safe_filename(suggested_filename))
        if self.download_directory.is_dir():
            chooser.set_current_folder(str(self.download_directory))
        info["chooser"] = chooser
        self.dialogs.add(chooser)

        def response(dialog, code):
            try:
                path = dialog.get_filename() if code == Gtk.ResponseType.ACCEPT else None
                if not path:
                    info["cancelled"] = True
                    download.cancel()
                    return
                destination = Path(path).absolute()
                info["path"] = destination
                self.download_directory = destination.parent
                # GtkFileChooserNative has already confirmed any replacement.
                download.set_allow_overwrite(True)
                download.set_destination(destination.as_uri())
                self._notice(f"正在保存：{destination.name}")
            except (GLib.Error, OSError, ValueError):
                info["failed"] = True
                download.cancel()
                self._notice("无法保存到所选位置，请重新导出并选择可写入的文件夹。", True)
            finally:
                info["chooser"] = None
                self.dialogs.discard(dialog)
                dialog.destroy()
        chooser.connect("response", response)
        if WEBKIT_VERSION >= (2, 40, 0):
            chooser.show()
        else:
            # Before 2.40 the destination must be set before this signal
            # returns. The native dialog's nested loop keeps GTK responsive.
            chooser.run()
        return True

    def _download_failed(self, download, error):
        info = self.downloads.get(download)
        if not info:
            return
        already_reported = info["failed"]
        info["failed"] = True
        if error.matches(WebKit2.download_error_quark(), WebKit2.DownloadError.CANCELLED_BY_USER):
            info["cancelled"] = True
        if not info["cancelled"] and not already_reported:
            self._notice("文档保存失败。请检查磁盘空间和文件夹权限，再重新导出。", True)

    def _download_finished(self, download):
        info = self.downloads.pop(download, None)
        if not info:
            return
        chooser = info.get("chooser")
        if chooser:
            self.dialogs.discard(chooser)
            chooser.destroy()
        if info["failed"] or info["cancelled"] or not info["path"]:
            return
        path = info["path"]
        self._notice(f"文档已保存：{path}")
        notification = Gio.Notification.new("报销文档已保存")
        notification.set_body(path.name)
        notification.set_icon(Gio.ThemedIcon.new("document-save-symbolic"))
        self.send_notification("receipt-download", notification)

    def _open_download_folder(self, *_args):
        try:
            self.download_directory.mkdir(parents=True, exist_ok=True)
            Gio.AppInfo.launch_default_for_uri(self.download_directory.as_uri(),
                self.window.get_display().get_app_launch_context())
        except (GLib.Error, OSError):
            self._notice("无法打开下载文件夹，请检查文件夹是否存在，以及系统的默认文件管理器设置。", True)

    def _on_key_press(self, _window, event):
        if not event.state & Gdk.ModifierType.CONTROL_MASK:
            return False
        key = event.keyval
        if key in (Gdk.KEY_r, Gdk.KEY_R):
            self._reload()
            return True
        if key in (Gdk.KEY_plus, Gdk.KEY_equal, Gdk.KEY_KP_Add):
            self.webview.set_zoom_level(min(2.0, self.webview.get_zoom_level() + .1))
            return True
        if key in (Gdk.KEY_minus, Gdk.KEY_KP_Subtract):
            self.webview.set_zoom_level(max(.6, self.webview.get_zoom_level() - .1))
            return True
        if key in (Gdk.KEY_0, Gdk.KEY_KP_0):
            self.webview.set_zoom_level(1.0)
            return True
        if key in (Gdk.KEY_v, Gdk.KEY_V):
            Gtk.Clipboard.get(Gdk.SELECTION_CLIPBOARD).request_image(self._clipboard_image)
            return True
        return False

    def _clipboard_image(self, _clipboard, pixbuf, _data=None):
        if pixbuf is None:
            self.webview.execute_editing_command(WebKit2.EDITING_COMMAND_PASTE)
            return
        try:
            success, png = pixbuf.save_to_bufferv("png", [], [])
            if not success:
                raise ValueError("PNG encoding failed")
            encoded = base64.b64encode(png).decode("ascii")
            filename = f"粘贴截图-{datetime.now():%Y%m%d-%H%M%S}.png"
            script = """(() => {
                if (document.querySelector('dialog[open]')) return 'dialog-open';
                const raw = atob(%s);
                const bytes = Uint8Array.from(raw, char => char.charCodeAt(0));
                const data = new DataTransfer();
                data.items.add(new File([bytes], %s, {type: 'image/png'}));
                document.dispatchEvent(new ClipboardEvent('paste', {
                    clipboardData: data, bubbles: true, cancelable: true
                }));
                return 'pasted';
            })();""" % (json.dumps(encoded), json.dumps(filename))
            self._evaluate_javascript(script, self._clipboard_pasted)
        except (GLib.Error, ValueError, TypeError):
            self._notice("无法读取剪贴板中的图片，请保存截图后使用「选择文件」导入。", True)

    def _clipboard_pasted(self, view, result, _data=None):
        try:
            value = self._javascript_result(view, result).to_string()
            if value == "dialog-open":
                self._notice("请先关闭当前对话框，再粘贴截图。")
        except GLib.Error:
            self._notice("图片粘贴未完成，请保存截图后使用「选择文件」导入。", True)

    def _on_close(self, *_args):
        self.closing = True
        for context in list(self.drop_transfers):
            self._finish_native_drop(context, False)
        for download in list(self.downloads):
            self.downloads[download]["cancelled"] = True
            download.cancel()
        for dialog in list(self.dialogs):
            dialog.destroy()
        self.dialogs.clear()
        return False


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="凭证归档 Linux 桌面窗口")
    parser.add_argument("--url", required=True, type=local_url, help="已启动的本机服务地址")
    parser.add_argument("--batch-id", type=batch_id, help="首次打开时恢复的本机批次编号")
    args = parser.parse_args(argv)
    GLib.set_application_name(APP_NAME)
    GLib.set_prgname("receipt-archive")
    try:
        app = ReceiptApplication(args.url, args.batch_id)
        return app.run([sys.argv[0]])
    except (GLib.Error, OSError) as exc:
        print(f"无法打开凭证归档桌面窗口：{exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
