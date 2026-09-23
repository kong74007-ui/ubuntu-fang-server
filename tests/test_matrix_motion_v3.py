import json
import unittest
from unittest import mock

from server import matrix_motion_v3 as v3
from server import matrix_template_api as api


def plan():
    return {'version':1,'audio_duration':12.,'duration':12.6,'audio_fingerprint':'a'*64,
            'cues':[{'text':'我在广州','en':'Here in Guangzhou','times':[.3,.6,.9,1.2],'end':2.,'row':0,'yellow':[2,3]}]}


class MotionV3Tests(unittest.TestCase):
    def test_owned_materials_keep_frame_precise_windows(self):
        service=object.__new__(api.MatrixTemplateService)
        service.reference_templates={}
        for template_id in (v3.OPENING,v3.BILINGUAL):
            payload={'template_id':template_id,'duration':17.3,'top_text':'广州圈子','bottom_text':'共同成长','bgm':False}
            if template_id==v3.BILINGUAL:
                payload['narration_plan']=plan()
                payload['duration']=12.6
            config=v3.runtime_config(api.FIXED_SKILL_TEMPLATE_CONFIGS[template_id],payload)
            payload['user_materials']=[{'sha256':f'{i:064x}','media_type':'video'} for i in range(config['required_visuals'])]
            with mock.patch.object(service,'user_asset_path',return_value='fixture'),mock.patch.object(service,'_inspect_user_asset',return_value=20.):
                materials=service._user_materials(payload)
            self.assertEqual([round(n/30,6) for n in config['slot_frames']], [m['clip_duration_seconds'] for m in materials])

    def test_three_templates_keep_original_timing_and_asset_mapping(self):
        c=api.FIXED_SKILL_TEMPLATE_CONFIGS
        self.assertEqual(443,c[v3.INSET]['frames'])
        self.assertEqual(519,c[v3.OPENING]['frames'])
        self.assertEqual(7,c[v3.INSET]['required_visuals'])
        self.assertEqual(4,c[v3.OPENING]['required_visuals'])
        self.assertEqual(2,len(c[v3.OPENING]['fixed_assets']))
        self.assertEqual('',c[v3.BILINGUAL]['bgm_path'])
        self.assertEqual('narration',c[v3.BILINGUAL]['duration_mode'])
        self.assertEqual(128,c[v3.BILINGUAL]['field_specs']['title']['minimum'])
        self.assertEqual(80,c[v3.BILINGUAL]['field_specs']['cta']['minimum'])

    def test_bilingual_duration_and_material_count_follow_audio(self):
        p=plan();config=v3.runtime_config(api.FIXED_SKILL_TEMPLATE_CONFIGS[v3.BILINGUAL],{'template_id':v3.BILINGUAL,'narration_plan':p})
        self.assertEqual(378,config['frames'])
        self.assertEqual(5,config['required_visuals'])
        self.assertEqual(12.6,config['ends'][-1])
        for end,start in zip(config['ends'],config['starts'][1:]):
            self.assertAlmostEqual(.18,end-start,places=5)
        self.assertEqual(3,api.FIXED_SKILL_TEMPLATE_CONFIGS[v3.BILINGUAL]['required_visuals'])

    def test_invalid_or_estimated_timing_contract_fails_closed(self):
        for mutate in (
            lambda p:p.update(duration=13),lambda p:p.update(audio_fingerprint='bad'),
            lambda p:p['cues'][0].update(times=[0]),lambda p:p['cues'][0].update(times=[0,.4,.2,.5]),
            lambda p:p['cues'][0].update(end=1.21),lambda p:p['cues'][0].update(row=True),
            lambda p:p['cues'][0].update(yellow=[99]),lambda p:p.update(audio_duration=float('nan')),
        ):
            p=plan();mutate(p)
            with self.assertRaises(ValueError):v3.validate_plan(p)

    def test_bilingual_preflight_can_measure_but_creation_requires_plan(self):
        service=object.__new__(api.MatrixTemplateService)
        service.fixed_skill_templates={v3.BILINGUAL:{}}
        payload={'template_id':v3.BILINGUAL,'semantic_layout':{},'top_text':'广州圈子','bottom_text':'共同成长','bgm':False}
        with self.assertRaisesRegex(api.MatrixTemplateError,'字幕时间轴'):
            service._freeze_font_provenance('a'*32,payload)

    def test_bilingual_build_keeps_phase_titles_not_extra_cta(self):
        markup='<html><head></head><body>__TITLE_HTML__ __TITLE_MOTION__ __MEDIA_HTML__ __MEDIA_MOTION__ __DURATION__ <div id="captions"></div><audio src="__VOICE_PATH__"></audio></body></html>'
        p=plan();config=v3.runtime_config(api.FIXED_SKILL_TEMPLATE_CONFIGS[v3.BILINGUAL],{'template_id':v3.BILINGUAL,'narration_plan':p})
        result=v3.build_bilingual(markup,{'title':'广州圈子','subtitle':'交流成长','body':'','cta':'共同成长'}, {},p,config)
        self.assertIn('heading-main0',result)
        self.assertIn('heading-main1',result)
        self.assertIn('heading-sub0',result)
        self.assertNotIn('website-cta',result)
        self.assertNotIn('<audio',result)
        self.assertIn('data-var-text="timed_captions"',result)
        self.assertNotIn('__MEDIA',result)

    def test_gpu_contract_covers_new_templates(self):
        from pathlib import Path
        contract=json.loads((Path(__file__).resolve().parents[1]/'deploy/matrix-gpu/contract.json').read_text())
        self.assertTrue(set(v3.IDS)<=set(contract['templates']))


if __name__=='__main__':unittest.main()
