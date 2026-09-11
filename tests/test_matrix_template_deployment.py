from __future__ import annotations

import hashlib
import html
import importlib.util
import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class MatrixTemplateDeploymentTests(unittest.TestCase):
    def test_installer_pins_skill_and_uses_atomic_release_contract(self):
        installer = (ROOT / "deploy/matrix-template-video/install.sh").read_text(encoding="utf-8")
        self.assertIn('UPSTREAM_COMMIT="243d5c168d9ab2d95daf04fef5c5e75924114eb8"', installer)
        self.assertIn('REFERENCE_UPSTREAM_COMMIT="9040a24139372f14346816cf42a97271767a0777"', installer)
        self.assertIn('NINE_GRID_UPSTREAM_COMMIT="2a2db5877728dcf4987f85973cfba38bb80f45a2"', installer)
        self.assertIn('MOTION_V2_HYPERFRAMES_VERSION="0.8.34"', installer)
        self.assertIn('HYPERFRAMES_VERSION="0.8.16"', installer)
        self.assertIn('NINE_GRID_HYPERFRAMES_VERSION="0.8.33"', installer)
        self.assertIn('GSAP_VERSION="3.14.2"', installer)
        self.assertIn('LAYOUT_PATCH_SHA256="33f64143e481301bcfd0f157ce1398c590d2e41512e2ea930772d739b4651329"', installer)
        self.assertIn('REFERENCE_LAYOUT_PATCH_SHA256="937507be0acff2132c8e5dac3ad89590795cba17db65cb168588d9b0381d3a2e"', installer)
        self.assertIn('NINE_GRID_ADAPTER_SHA256="b0b60138b6d51d8b1fa672f9552dae1fbc3c96e387de2a072e6cf7eb655b75cd"', installer)
        self.assertIn('NINE_GRID_PACKAGE_SHA256="6a9f7d9900b2a7e9c451811b19f373fa2a081f3737133c5783346aeebc0be216"', installer)
        self.assertIn('NINE_GRID_LOCK_SHA256="df5d53aa4b5c3e8cf0c896649b3ea8c75c5d76d197ebc89d2923d12964423e84"', installer)
        self.assertIn(
            'git -C "${RELEASE}/upstream" apply --check --directory=script-to-matrix-video',
            installer,
        )
        self.assertIn(
            'git -C "${RELEASE}/upstream" apply --directory=script-to-matrix-video',
            installer,
        )
        self.assertIn(
            'git -C "${REFERENCE_UPSTREAM}" apply --check "${REFERENCE_LAYOUT_PATCH_SOURCE}"',
            installer,
        )
        self.assertIn(
            'git -C "${REFERENCE_UPSTREAM}" apply "${REFERENCE_LAYOUT_PATCH_SOURCE}"',
            installer,
        )
        self.assertIn(
            'python3 "${REFERENCE_V04_PREVIEW_CHECK_SOURCE}"', installer,
        )
        self.assertIn('--pack-root "${REFERENCE_PACK_ROOT}"', installer)
        self.assertIn('--browser "${HYPERFRAMES_BROWSER}"', installer)
        self.assertIn('python3 "${SKILL_ROOT}/scripts/test_private_domain_layouts.py"', installer)
        self.assertIn('python3 "${SKILL_ROOT}/scripts/test_private_domain_catalog.py"', installer)
        self.assertIn('python3 "${SKILL_ROOT}/scripts/restrict_private_domain_catalog.py"', installer)
        self.assertIn('PRIVATE_FONT_ROOT="${STATE_ROOT}/private-fonts"', installer)
        self.assertIn('MATRIX_TEMPLATE_PRIVATE_FONT_ROOT=${PRIVATE_FONT_ROOT}', installer)
        self.assertIn('MATRIX_TEMPLATE_REFERENCE_SKILL_ROOT=${SOURCE_LINK}/reference-upstream/script-to-matrix-video', installer)
        self.assertIn('MATRIX_TEMPLATE_NINE_GRID_ROOT=${SOURCE_LINK}/nine-grid-upstream/script-to-matrix-video/assets/templates/nine-grid-reveal', installer)
        self.assertIn('MATRIX_TEMPLATE_TRIPLE_STRIP_ROOT=${SOURCE_LINK}/nine-grid-upstream/script-to-matrix-video/assets/templates/triple-strip-shutter', installer)
        self.assertIn('MATRIX_TEMPLATE_YELLOW_BANNER_ROOT=${SOURCE_LINK}/nine-grid-upstream/script-to-matrix-video/assets/templates/yellow-banner-zoom', installer)
        self.assertIn('MATRIX_TEMPLATE_FAN_WHIP_ROOT=${SOURCE_LINK}/nine-grid-upstream/script-to-matrix-video/assets/templates/fan-whip-static', installer)
        self.assertIn('MATRIX_TEMPLATE_BRUSH_PANEL_ROOT=${SOURCE_LINK}/nine-grid-upstream/script-to-matrix-video/assets/templates/brush-panel-transitions', installer)
        self.assertIn('python3 "${NINE_GRID_UPSTREAM}/script-to-matrix-video/scripts/test_triple_strip.py"', installer)
        self.assertIn('python3 "${NINE_GRID_UPSTREAM}/script-to-matrix-video/scripts/test_yellow_banner.py"', installer)
        self.assertIn('MATRIX_TEMPLATE_HYPERFRAMES_CLI=${HYPERFRAMES_CLI}', installer)
        self.assertIn('MATRIX_TEMPLATE_NINE_GRID_HYPERFRAMES_CLI=${SOURCE_LINK}/nine-grid-runtime/hyperframes', installer)
        self.assertIn('MATRIX_TEMPLATE_MOTION_V2_HYPERFRAMES_CLI=${SOURCE_LINK}/motion-v2-runtime/hyperframes', installer)
        self.assertIn('MATRIX_TEMPLATE_HYPERFRAMES_CONCURRENCY=2', installer)
        self.assertIn('MATRIX_TEMPLATE_HYPERFRAMES_TOTAL_TIMEOUT_SECONDS=900', installer)
        self.assertIn('MATRIX_TEMPLATE_HYPERFRAMES_SLOT_TIMEOUT_SECONDS=600', installer)
        self.assertIn('MATRIX_TEMPLATE_CONCURRENCY=5', installer)
        self.assertIn('d.get("concurrency")==5', installer)
        self.assertIn('d.get("worker_count")==5', installer)
        self.assertIn('requires at least 4 vCPU and 7 GiB RAM', installer)
        self.assertIn('install -o root -g admin -m 0640 "${BACKUP}/env" "${ENV_FILE}"', installer)
        self.assertIn('git -C "${RELEASE}/upstream" rev-parse HEAD', installer)
        self.assertIn('systemctl stop "${SERVICE}"', installer)
        self.assertIn('systemctl start "${SERVICE}"', installer)
        self.assertIn('d.get("build_id")==os.environ["EXPECTED_BUILD_ID"]', installer)
        self.assertIn('d.get("templates")==22', installer)
        self.assertIn('d.get("hyperframes_templates")==17', installer)
        self.assertIn('d.get("hyperframes_version")=="0.8.16"', installer)
        self.assertIn('d.get("nine_grid_templates")==1', installer)
        self.assertIn('d.get("nine_grid_hyperframes_version")=="0.8.33"', installer)
        self.assertIn('d.get("fixed_skill_templates")==["brush-panel-transitions","fan-whip-static","triple-strip-shutter","yellow-banner-zoom"]', installer)
        self.assertIn('d.get("fixed_skill_template_count")==4', installer)
        self.assertIn('d.get("fixed_skill_hyperframes_version")=="mixed"', installer)
        self.assertIn('d.get("fixed_skill_hyperframes_versions")=={"0.8.33":2,"0.8.34":2}', installer)
        self.assertIn('d.get("reference_top_layer_counts")=={"2":6,"3":10,"4":1}', installer)
        self.assertIn('d.get("reference_fixed_private_fonts")==["Smiley Sans Oblique"]', installer)
        self.assertIn(
            'd.get("reference_semantic_layout_templates")==["v01","v02","v03","v04","v05","v06","v07","v08","v09","v10","v11","v12","v13","v14","v15","v16","v17"]',
            installer,
        )
        self.assertIn('r"(?m)^\\s*#root \\.top', installer)
        self.assertIn("assert top_safe_area is not None", installer)
        self.assertIn("from PIL import Image, ImageDraw, ImageFont", installer)
        self.assertIn('sample = "AI视频获客增长100条"', installer)
        self.assertIn("font.set_variation_by_axes(values)", installer)
        self.assertIn("widths[400] <= 996 < widths[900]", installer)
        self.assertIn('xiaowei_sample = "MMMMMMMMMMMMMM"', installer)
        self.assertIn("970 < xiaowei_width <= 996", installer)
        self.assertIn('d.get("max_batch_size")==5', installer)
        self.assertIn('d.get("engine_concurrency")=={"ffmpeg":5,"hyperframes":2}', installer)
        self.assertIn('d.get("hyperframes_concurrency")==2', installer)
        self.assertIn('d.get("hyperframes_total_timeout_seconds")==900', installer)
        self.assertIn('d.get("hyperframes_slot_timeout_seconds")==600', installer)
        self.assertIn('d.get("material_library_ready") is True', installer)
        self.assertIn(
            'd.get("material_selection_contract_version")==2', installer,
        )
        self.assertIn('d.get("material_clip_contract_version")==3', installer)
        self.assertNotIn("MATRIX_TEMPLATE_API_TOKEN=sk-", installer)
        self.assertIn("MATRIX_TEMPLATE_RETENTION_SECONDS=259200", installer)
        self.assertIn("MATRIX_TEMPLATE_DELIVERY_GRACE_SECONDS=3600", installer)
        self.assertIn("MATRIX_TEMPLATE_CLEANUP_INTERVAL_SECONDS=900", installer)
        self.assertIn("MATRIX_TEMPLATE_CLEANUP_BATCH_SIZE=10", installer)
        self.assertIn("MATRIX_TEMPLATE_DISK_HIGH_WATER_PERCENT=95", installer)
        self.assertIn('"${NODE_NPM}" ci', installer)
        self.assertIn('"${NODE_NPM}" ls', installer)
        self.assertIn('--ignore-scripts --no-audit --no-fund', installer)

    def test_nine_grid_runtime_lock_is_complete_and_integrity_pinned(self):
        root = ROOT / "deploy/matrix-template-video/nine-grid-runtime"
        package = json.loads((root / "package.json").read_text(encoding="utf-8"))
        lock_path = root / "package-lock.json"
        lock = json.loads(lock_path.read_text(encoding="utf-8"))
        self.assertEqual("0.8.33", package["dependencies"]["hyperframes"])
        self.assertEqual(3, lock["lockfileVersion"])
        self.assertEqual(
            {"hyperframes": "0.8.33"}, lock["packages"][""]["dependencies"],
        )
        self.assertEqual(
            "0.8.33", lock["packages"]["node_modules/hyperframes"]["version"],
        )
        registry_packages = [
            item for key, item in lock["packages"].items()
            if key and isinstance(item, dict)
            and str(item.get("resolved") or "").startswith("https://registry.npmjs.org/")
        ]
        self.assertTrue(registry_packages)
        self.assertTrue(all(item.get("integrity") for item in registry_packages))
        self.assertEqual(
            "6a9f7d9900b2a7e9c451811b19f373fa2a081f3737133c5783346aeebc0be216",
            hashlib.sha256((root / "package.json").read_bytes()).hexdigest(),
        )
        self.assertEqual(
            "df5d53aa4b5c3e8cf0c896649b3ea8c75c5d76d197ebc89d2923d12964423e84",
            hashlib.sha256(lock_path.read_bytes()).hexdigest(),
        )

    def test_motion_v2_runtime_lock_is_complete_and_integrity_pinned(self):
        root = ROOT / "deploy/matrix-template-video/motion-v2-runtime"
        package = json.loads((root / "package.json").read_text(encoding="utf-8"))
        lock_path = root / "package-lock.json"
        lock = json.loads(lock_path.read_text(encoding="utf-8"))
        self.assertEqual("0.8.34", package["dependencies"]["hyperframes"])
        self.assertEqual(3, lock["lockfileVersion"])
        self.assertEqual(
            {"hyperframes": "0.8.34"},
            lock["packages"][""]["dependencies"],
        )
        self.assertEqual(
            "0.8.34",
            lock["packages"]["node_modules/hyperframes"]["version"],
        )
        registry_packages = [
            item for key, item in lock["packages"].items()
            if key and isinstance(item, dict)
            and str(item.get("resolved") or "").startswith(
                "https://registry.npmjs.org/"
            )
        ]
        self.assertTrue(registry_packages)
        self.assertTrue(all(item.get("integrity") for item in registry_packages))
        self.assertEqual(
            "3f0d57a4c19af984134511451ca7acc402cab99d845107b5d316ed466466b65e",
            hashlib.sha256((root / "package.json").read_bytes()).hexdigest(),
        )
        self.assertEqual(
            "c727689682957da2372f900c1d9ea77cbc5a1cf407765959cc3b750fceb4945e",
            hashlib.sha256(lock_path.read_bytes()).hexdigest(),
        )

    def test_nine_grid_adapter_rewrites_copy_and_fullscreen_contract(self):
        path = ROOT / "deploy/matrix-template-video/prepare-nine-grid-template.py"
        self.assertEqual(
            "b0b60138b6d51d8b1fa672f9552dae1fbc3c96e387de2a072e6cf7eb655b75cd",
            hashlib.sha256(path.read_bytes()).hexdigest(),
        )
        spec = importlib.util.spec_from_file_location(
            "prepare_nine_grid_template", path,
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            variables = [
                {"id": "title", "type": "string", "label": "title",
                 "default": "输入公司名称", "maxLength": 9},
                {"id": "tagline", "type": "string", "label": "tagline",
                 "default": "品质｜细节｜诚信｜口碑", "maxLength": 16},
            ]
            schema = html.escape(json.dumps(variables), quote=True)
            videos = "".join(
                f'<video id="main-video{index}" data-media-start="{value}"'
                + (
                    ' data-color-grading="{}" '
                    'style="--hf-color-grading-blur:0.1"'
                    if index == 1 else ""
                )
                + '></video>'
                for index, value in enumerate((3.2, 2, 2), 1)
            )
            videos = videos.replace(
                '<video id="main-video1"',
                '<div id="main1-visual" class="main-visual">'
                '<video id="main-video1"',
                1,
            ).replace("</video>", "</video></div>", 1)
            (root / "index.html").write_text(
                f'<html data-composition-variables="{schema}"><head></head><body>'
                '<span id="title" data-var-text="title">输入公司名称</span>'
                '<div id="tagline" data-var-text="tagline">'
                f'品质｜细节｜诚信｜口碑</div>{videos}'
                "<script>tl.set('#main-video1',"
                "{'--hf-color-grading-blur':0},97/30);</script>"
                "</body></html>",
                encoding="utf-8",
            )
            (root / "template.json").write_text(json.dumps({
                "id": "nine-grid-reveal", "version": 3,
                "renderer": "hyperframes@0.8.33",
                "canvas": [1080, 1920], "fps": 30, "duration": 12,
                "text_fields": ["title", "tagline"],
                "text_limits": {"title": 9, "tagline": 16},
            }), encoding="utf-8")

            module.adapt(root)

            manifest = json.loads(
                (root / "template.json").read_text(encoding="utf-8")
            )
            index = (root / "index.html").read_text(encoding="utf-8")
            self.assertEqual(4, manifest["version"])
            self.assertEqual(
                ["top_text", "bottom_text"], manifest["text_fields"],
            )
            self.assertEqual(
                {"top_text": 60, "bottom_text": 80},
                manifest["text_limits"],
            )
            self.assertIn('data-var-text="top_text"', index)
            self.assertIn('data-var-text="bottom_text"', index)
            self.assertIn('id="matrix-nine-grid-copy-layout"', index)
            self.assertEqual(3, index.count('data-media-start="0"'))
            self.assertNotIn("data-color-grading", index)
            self.assertNotIn("--hf-color-grading-blur", index)
            self.assertIn("filter:'blur(10px)'", index)
            self.assertIn("data-layout-allow-overflow", index)

    def test_reference_patch_is_hash_locked_and_sets_all_top_offsets_to_eight_percent(self):
        patch_path = (
            ROOT / "deploy/matrix-template-video/reference-featured-layout.patch"
        )
        patch = patch_path.read_text(encoding="utf-8")
        self.assertEqual(
            "937507be0acff2132c8e5dac3ad89590795cba17db65cb168588d9b0381d3a2e",
            hashlib.sha256(patch_path.read_bytes()).hexdigest(),
        )
        self.assertIn(
            "script-to-matrix-video/assets/templates/reference-typography-17/index.html",
            patch,
        )
        self.assertIn('font: 900 102px/1.02 "NotoSC";', patch)
        self.assertIn("-webkit-text-stroke: 12px #203449;", patch)
        self.assertIn("background: #f4c900;", patch)
        self.assertIn('font: 400 70px/1.08 "MaShan";', patch)
        self.assertIn('font: 400 64px/1.15 "MaShan";', patch)
        self.assertIn("font-size: 52px;", patch)
        self.assertIn('font: 400 56px/1.05 "MaShan";', patch)
        self.assertIn('font: 400 74px/1.15 "MaShan";', patch)
        self.assertIn("max-width: 996px;", patch)
        self.assertIn(".v06 .top1 { font-size: 86px;", patch)
        self.assertIn(".v08 .top1 { font-size: 86px;", patch)
        self.assertIn(".v15 .bottom2 { font-size: 82px;", patch)
        self.assertIn(
            '.v04 .bottom2 { font-size: 80px; font-weight: 900;', patch,
        )
        self.assertIn(
            '"bottom2": "交友破圈｜信息差｜\\n自媒体｜AI智能体"',
            patch,
        )
        self.assertIn("#root .top { top: 8%; }", patch)
        self.assertIn('font: 400 88px/1.08 "MaShan";', patch)
        self.assertIn('font: 400 85px/1.02 "XiaoWei";', patch)
        self.assertIn('font-size: 65px; font-weight: 800;', patch)
        self.assertIn(
            '"top3": "自媒体｜AI沙龙｜\\n抄经｜睡眠沙龙"', patch,
        )
        self.assertIn('font: 400 80px/1.08 "MaShan";', patch)
        self.assertIn('font: 400 70px/1.12 "MaShan";', patch)
        self.assertIn('font: 400 80px/1.1 "XiaoWei";', patch)
        self.assertIn(
            '"top1": "我在深圳发起了共享办公\\n共享创业 OPC 自媒体平台"',
            patch,
        )
        self.assertIn("font-size: 118px;", patch)
        self.assertIn("color: #d4140d;", patch)
        self.assertIn("-webkit-text-stroke: 13px #ffe9be;", patch)
        self.assertIn("color: #ffd51c;", patch)
        self.assertIn("-webkit-text-stroke: 11px #101010;", patch)
        self.assertIn("font-size: 57px;", patch)
        self.assertIn("font-size: 86px;", patch)
        self.assertIn('if (vars.variant === "v07")', patch)
        self.assertIn('"top1": "999元成为会员"', patch)
        self.assertIn('"bottom1": "链接1000位深圳湾沙龙主理人"', patch)
        self.assertNotIn(".v02 .top1 {\n+", patch)
        self.assertNotIn(".v04 .top1 {\n+", patch)
        self.assertNotIn(".v06 .top1 {\n+", patch)

    def test_v04_preview_browser_guard_rejects_visual_regressions(self):
        path = ROOT / "deploy/matrix-template-video/verify_v04_preview.py"
        spec = importlib.util.spec_from_file_location("verify_v04_preview", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        valid = {
            "font_loaded": True,
            "font_size": "80px",
            "lines": module.BOTTOM2_LINES,
            "clipped": False,
            "overlap": False,
        }
        module.validate_report(valid)
        invalid = (
            dict(valid, font_loaded=False),
            dict(valid, font_size="60px"),
            dict(valid, lines=["交友破圈｜信息差｜自媒体｜AI智能", "体"]),
            dict(valid, clipped=True),
            dict(valid, overlap=True),
        )
        for report in invalid:
            with self.subTest(report=report), self.assertRaises(RuntimeError):
                module.validate_report(report)
        source = path.read_text(encoding="utf-8")
        self.assertIn("--headless=new", source)
        self.assertIn("document.fonts.check", source)
        self.assertIn("getBoundingClientRect", source)

    def test_v07_preview_browser_guard_rejects_visual_regressions(self):
        path = ROOT / "deploy/matrix-template-video/verify_v07_preview.py"
        spec = importlib.util.spec_from_file_location("verify_v07_preview", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)

        def layer(color, size, stroke, left, right, top, bottom):
            return {
                "display": "block", "visibility": "visible",
                "opacity": "1", "color": color, "fontSize": size,
                "strokeColor": stroke,
                "rect": {"left": left, "right": right,
                         "top": top, "bottom": bottom},
                "width": right - left, "height": bottom - top,
            }

        valid = {
            "font_loaded": True,
            "stage": {"left": 0, "right": 1080, "top": 0, "bottom": 1920},
            "layers": {
                "top1": layer("rgb(212, 20, 13)", "118px", "rgb(255, 233, 190)", 100, 980, 154, 300),
                "top2": layer("rgb(255, 213, 28)", "82px", "rgb(16, 16, 16)", 200, 880, 320, 420),
                "top3": layer("rgb(255, 213, 28)", "51px", "rgb(16, 16, 16)", 150, 930, 440, 510),
                "bottom1": layer("rgb(212, 20, 13)", "57px", "rgb(255, 233, 190)", 180, 900, 530, 600),
                "bottom2": layer("rgb(212, 20, 13)", "86px", "rgb(255, 233, 190)", 300, 780, 1400, 1500),
            },
        }
        module.validate_report(valid)
        invalid_cases = (
            dict(valid, font_loaded=False),
            dict(valid, layers={
                **valid["layers"],
                "top1": layer("rgb(212, 20, 13)", "104px", "rgb(255, 233, 190)", 100, 980, 154, 300),
            }),
            dict(valid, layers={
                **valid["layers"],
                "top2": layer("rgb(255, 255, 255)", "82px", "rgb(16, 16, 16)", 200, 880, 320, 420),
            }),
            dict(valid, layers={
                **valid["layers"],
                "top1": {**valid["layers"]["top1"], "display": "none"},
            }),
            dict(valid, layers={
                **valid["layers"],
                "top1": {**valid["layers"]["top1"], "opacity": "0"},
            }),
            dict(valid, layers={
                **valid["layers"],
                "top1": layer("rgb(212, 20, 13)", "118px", "rgb(255, 233, 190)", 20, 980, 154, 300),
            }),
            dict(valid, layers={
                **valid["layers"],
                "top2": layer("rgb(255, 213, 28)", "82px", "rgb(16, 16, 16)", 200, 880, 290, 420),
            }),
            dict(valid, layers={
                **valid["layers"],
                "bottom2": layer("rgb(212, 20, 13)", "86px", "rgb(255, 233, 190)", 300, 780, 1400, 1660),
            }),
        )
        for report in invalid_cases:
            with self.subTest(report=report), self.assertRaises(RuntimeError):
                module.validate_report(report)
        source = path.read_text(encoding="utf-8")
        self.assertIn("--headless=new", source)
        self.assertIn("document.fonts.check", source)
        self.assertIn("getBoundingClientRect", source)
        self.assertIn('"v07"', source)

    def test_installer_runs_v07_preview_guard(self):
        installer = (
            ROOT / "deploy/matrix-template-video/install.sh"
        ).read_text(encoding="utf-8")
        self.assertIn('REFERENCE_V07_PREVIEW_CHECK_SOURCE="${DEPLOY_ROOT}/deploy/matrix-template-video/verify_v07_preview.py"', installer)
        self.assertIn('"${REFERENCE_V07_PREVIEW_CHECK_SOURCE}"', installer)
        self.assertIn('python3 "${REFERENCE_V07_PREVIEW_CHECK_SOURCE}"', installer)

    def test_installer_checks_v12_v16_target_copy_widths(self):
        installer = (
            ROOT / "deploy/matrix-template-video/install.sh"
        ).read_text(encoding="utf-8")
        self.assertIn('PRIVATE_FONT_ROOT="${PRIVATE_FONT_ROOT}" python3 -', installer)
        self.assertIn('smiley_path = Path(os.environ["PRIVATE_FONT_ROOT"])', installer)
        for value in (
            '"v10.top1"', '"v10.top3"', '"v12.top1"', '"v12.top3"', '"v16.top1"',
            '"v16.top2"', '"v16.bottom1"', '"v16.bottom2"',
        ):
            self.assertIn(value, installer)
        self.assertIn("assert max(widths) <= 996", installer)
        self.assertIn("weight=800", installer)

    def test_systemd_is_loopback_hardened_and_reuses_material_tunnel(self):
        unit = (ROOT / "deploy/systemd/huangque-matrix-template.service").read_text(encoding="utf-8")
        self.assertIn("--host 127.0.0.1 --port 8112", unit)
        self.assertIn("Requires=huangque-pixelle-material-tunnel.service", unit)
        self.assertIn("EnvironmentFile=/etc/huangque/pixelle-material-library.env", unit)
        self.assertIn("NoNewPrivileges=true", unit)
        self.assertIn("ProtectSystem=strict", unit)
        self.assertIn("ReadWritePaths=/var/lib/huangque-matrix-template", unit)
        self.assertIn("MemoryMax=6G", unit)
        self.assertIn("CPUQuota=400%", unit)

    def test_nginx_bridge_is_private_to_production_content_host(self):
        for relative in (
            "deploy/nginx-fang-locations.conf",
            "deploy/nginx-huangquechuanmei.conf",
        ):
            nginx = (ROOT / relative).read_text(encoding="utf-8")
            self.assertIn("/internal/matrix-template/", nginx)
            self.assertIn("allow 129.204.166.13;", nginx)
            self.assertIn("deny all;", nginx)
            self.assertIn("proxy_pass http://127.0.0.1:8112/;", nginx)

    def test_installer_shell_syntax(self):
        bash = shutil.which("bash") or "D:/Git/bin/bash.exe"
        subprocess.run([
            bash, "-n", str(ROOT / "deploy/matrix-template-video/install.sh")
        ], check=True)


if __name__ == "__main__":
    unittest.main()
