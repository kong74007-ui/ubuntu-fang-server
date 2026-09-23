import copy
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from PIL import ImageFont
from server import matrix_template_api as api


class TextControlTests(unittest.TestCase):
    def test_layout_failure_reports_roles_not_the_end_of_a_javascript_stack(self):
        message=api.text_controls.render_error(b'',b'Error: Text layers overlap: top1,top2\n'+b'frame.js\n'*300)
        self.assertIn('top1,top2',message)
        self.assertIn('重叠',message)

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        for name in ("DejaVuSans.ttf", "Arial.ttf", "C:/Windows/Fonts/arial.ttf"):
            try:
                font = ImageFont.truetype(name, 16)
                break
            except OSError:
                continue
        else:
            self.skipTest("Test system font unavailable")
        self.font = self.root / "font.ttf"
        self.font.write_bytes(Path(font.path).read_bytes())
        self.font_record = {"family":"Noto Sans SC", "path":self.font,"file":"font.ttf","sha256":hashlib.sha256(self.font.read_bytes()).hexdigest()}
        self.markup = '<html><head><style>body{color:#eee}#root{--ink:#2468ab}.top1,.top2{color:var(--ink);-webkit-text-stroke:2px #123456}.v01 .top1{color:#abcdef!important}.bottom2{color:red}</style></head><body><div id="root" data-composition-id="fixture"><div data-var-text="top1" class="top1">Alpha</div><div data-var-text="top2" class="top2">Bravo</div><div data-var-text="bottom2" class="bottom2">Join now</div></div></body></html>'
        (self.root / "index.html").write_text(self.markup, encoding="utf-8")
        self.service = object.__new__(api.MatrixTemplateService)
        s = self.service
        s.reference_pack_root = self.root
        s.reference_fonts = {"Noto Sans SC":self.font_record}
        s.bundled_fonts = {};s.private_fonts = {};s.fixed_skill_fonts = {};s.nine_grid_fonts = {}
        s.nine_grid_template = None;s.fixed_skill_templates = {};s.reference_measure_fonts = {};s.nine_grid_measure_fonts = {}
        s.reference_font_fingerprint = api._font_bundle_fingerprint(s.reference_fonts)
        self.template_id = "ref-01-fixture"
        record = {"id":self.template_id,"variant":"v01","text_layers":{"top":2},"tunable":False}
        s.reference_templates = {self.template_id:record};s.templates = s.reference_templates
        s.default_template_id = self.template_id
        s.reference_semantic_layouts = {"v01":{layer:{"family":"Noto Sans SC","font_size_px":80,"font_weight":400,"max_width_px":900,"max_lines":2,"stroke_px":2,"letter_spacing_em":0} for layer in ("top1","top2","bottom2")}}
        top, bottom = "广州圈子，交流成长", "共同成长"
        self.body = {"template_id":self.template_id,"top_text":top,"bottom_text":bottom,"bgm":False,
                     "semantic_layout":{"version":1,"model":"fixture","source_sha256":api._reference_semantic_source_sha256(top,bottom),"top1_end":4,"top_break_after":[4],"bottom_break_after":[]}}

    def model(self):
        return self.service._text_control_model()

    def changed(self, values=None):
        return dict(self.body, text_revision=self.model().describe(self.template_id)["text_revision"],
                    text_overrides=values or {"top1":{"font_size_px":90,"color":"#fefefe"}})

    def test_describes_actual_cascade_and_no_paths(self):
        controls = self.model().describe(self.template_id)
        self.assertEqual("#ABCDEF",controls["layers"]["top1"]["defaults"]["color"])
        self.assertEqual("#2468AB",controls["layers"]["top2"]["defaults"]["color"])
        self.assertEqual("#123456",controls["layers"]["top1"]["defaults"]["stroke_color"])
        self.assertEqual("#FF0000",controls["layers"]["bottom2"]["defaults"]["color"])
        self.assertNotIn(str(self.root),json.dumps(controls))

    def test_no_changes_keep_old_payload_and_markup_identical(self):
        before = self.service.validate_payload(self.body)
        after = self.service.validate_payload(dict(self.body,text_overrides={}))
        self.assertEqual(before,after)
        self.assertEqual(self.markup,self.model().inject(before,self.markup,self.root))

    def test_invalid_inputs_fail_without_silent_clamping(self):
        for values in ({"unknown":{"color":"#ffffff"}},{"top1":{"font_family":"../../font"}},
                       {"top1":{"color":"red;display:none"}},{"top1":{"font_size_px":True}},
                       {"top1":{"font_size_px":999}},{"top1":{"font_size_px":90.5}},
                       {"top1":{"offset_y_px":float("nan")}},{"top1":{"css":"bad"}}):
            with self.subTest(values=values),self.assertRaises(ValueError):
                self.service.validate_payload(self.changed(values))
        with self.assertRaises(ValueError):self.service.validate_payload(dict(self.changed(),text_revision="a"*64))
        with self.assertRaises(ValueError):self.service.validate_payload(dict(self.changed(),overrides={"title_scale":1.1}))

    def test_request_views_do_not_mutate_shared_template_defaults(self):
        before = copy.deepcopy(self.service.reference_semantic_layouts)
        def process(size):
            raw = self.changed({"top1":{"font_size_px":size,"font_family":"Noto Sans SC"}})
            payload = self.service.validate_payload(raw)
            return self.service._freeze_font_provenance(str(size),payload)
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(process,(72,96)))
        self.assertEqual([72,96],[r["_text_style"]["overrides"]["top1"]["font_size_px"] for r in results])
        self.assertEqual(before,self.service.reference_semantic_layouts)
        self.assertFalse(hasattr(self.service,"_text_style_view"))

    def test_font_identity_frozen_and_styles_target_only_requested_layers(self):
        raw = self.changed({"top1":{"font_family":"Noto Sans SC","color":"#123abc","offset_y_px":-12,"stroke_width_px":3}})
        payload = self.service._freeze_font_provenance("fixture",self.service.validate_payload(raw))
        self.assertEqual("#123ABC",payload["text_overrides"]["top1"]["color"])
        html = self.model().inject(payload,self.markup,self.root/"output")
        self.assertIn("matrix-text-overrides",html)
        self.assertIn("MatrixText_"+self.font_record["sha256"],html)
        self.assertIn("__matrixValidateTextStyles",html)
        self.assertEqual(self.markup,(self.root/"index.html").read_text(encoding="utf-8"))
        self.font.write_bytes(b"changed")
        with self.assertRaisesRegex(ValueError,"字体校验失败"):
            self.model().inject(payload,self.markup,self.root/"output")

    def test_revision_changes_with_template_source_and_replay_uses_saved_contract(self):
        payload = self.service._freeze_font_provenance("fixture",self.service.validate_payload(self.changed()))
        raw = {k:v for k,v in payload.items() if not k.startswith("_")}
        (self.root/"index.html").write_text(self.markup+'\n<!-- new revision -->',encoding="utf-8")
        with self.assertRaises(ValueError):self.service.validate_payload(raw)
        self.service.store = mock.Mock()
        self.service.store.get_by_request_id.return_value = {"payload":json.dumps(payload)}
        self.service.store.create.return_value = ({"job_id":"already-existing"},False)
        job = self.service.submit(raw,"replay-fixture")
        self.assertEqual("already-existing",job["job_id"])
        self.assertEqual(raw["text_overrides"],self.service.store.create.call_args.args[1]["text_overrides"])

    def test_old_gpu_runtime_is_rejected_for_styled_jobs_only(self):
        self.service._require_text_gpu(self.body)
        with self.assertRaises(api.MatrixTemplateError):self.service._require_text_gpu(self.changed())
        self.service.gpu_runtime = mock.Mock(text_controls_contract_version=1)
        self.service._require_text_gpu(self.changed())


if __name__ == "__main__":unittest.main()
