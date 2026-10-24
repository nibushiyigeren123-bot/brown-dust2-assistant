"""Current two-pass recognition, separated from input and journaling."""
import json
import re
import cv2
from reward_overlay_vision import RewardOverlayVision
from common_navigation import PageDiagnosis
from daily_vision import OcrDocument, windows_ocr
from m00_recognize import read_image
from project_paths import ROOT


class PassRewardVision:
    def __init__(self, require_character_entry=False):
        # [阶段需求] 默认不追加角色入口重读；执行器仅在已启用的角色阶段打开。
        self.require_character_entry = require_character_entry
        self.reward_overlay = RewardOverlayVision()
        self.folder = ROOT / 'assets/pass_rewards'
        self.specs = json.loads((self.folder / 'sources.json').read_text(encoding='utf-8'))
        self.templates = {key: read_image(self.folder / spec['file']) for key, spec in self.specs.items()}
        from m00_recognize import OpeningRecognizer
        self.opening=OpeningRecognizer()

    def inspect(self, image, payload):
        if abs(image.shape[1] / image.shape[0] - 16 / 9) > .025:
            return {'state': 'unknown', 'reason': 'unsupported_aspect_ratio'}
        overlay = self.reward_overlay.inspect(image)
        if overlay:
            return overlay
        doc = OcrDocument(payload)
        startup=self.opening.recognize(image)
        if startup['state'] in ('signin_event','signin_pass','loading_or_unknown_overlay'):
            return {'state':startup['state'],'reason':'opening_overlay_is_not_pass_rewards'}
        if doc.find('点击画面即可返回', (400, 595, 900, 690)):
            return {'state': 'reward_overlay'}
        # [当前弹出页] 标题可能被worker隐私遮罩擦去；仍以可见页面结构及身份、页签原裁片定位。
        header=doc.find('通行证',(155,15,265,55))
        popup_title=any('通行证' in line['text'] for line in doc.within((370,180,620,430)))
        # [角色艺术区] PASS不要求姓名；仍须选中后缀入口和任务/等级控件结构共同成立。
        normalized=cv2.resize(image,(1280,720),interpolation=cv2.INTER_AREA)
        character_proof=self.character_page(doc,normalized,self.character_entry(doc,normalized))
        navigation_state = None
        if not header and not popup_title and not character_proof:
            navigation_state = PageDiagnosis().inspect(image)['state']
            # [worker接线] 共享导航的specific状态只作页面证据，不能直接当业务观察返回。
            # 导航已要求身份/页签裁片及其亮度可见；此处仍须继续原有选中裁片验证。
            if navigation_state not in ('pass_tasks_special', 'pass_levels_special',
                                        'pass_tasks_season', 'pass_levels_season'):
                return {'state': navigation_state}
        normalized=cv2.resize(image,(1280,720),interpolation=cv2.INTER_AREA)
        gray=cv2.cvtColor(normalized,cv2.COLOR_BGR2GRAY)
        if header and int((gray[21:49,184:258]>180).sum())<100:
            return {'state':'unknown','reason':'pass_title_dimmed_or_obscured'}
        scores = {}
        for key, spec in self.specs.items():
            x1, y1, x2, y2 = spec['box']
            scores[key] = float(cv2.matchTemplate(normalized[y1:y2, x1:x2], self.templates[key], cv2.TM_CCOEFF_NORMED)[0, 0])
        def best(prefix):
            ranked = sorted((k for k in scores if k.startswith(prefix)), key=scores.get, reverse=True)
            return ranked[0].split(':')[-1] if scores[ranked[0]] >= .97 and scores[ranked[0]] - scores[ranked[1]] >= .008 else None
        # [角色入口] 固定后缀定位，名称只作为可选metadata；选中描边与PASS结构独立证明当前页。
        entry=self.character_entry(doc,normalized)
        character=self.character_page(doc,normalized,entry)
        identity = 'character' if character else best('identity:')
        tab = character.get('tab') if character else best('tab:' + identity + ':') if identity else None
        if identity is None or tab is None:
            return {'state': 'unknown', 'scores': scores}
        if navigation_state and navigation_state != 'pass_' + tab + '_' + identity:
            return {'state': 'unknown', 'reason': 'pass_navigation_identity_mismatch', 'scores': scores}
        # [导航输出] 不识别任务进度、等级或按钮可领取状态；执行器只消费页面、身份与入口。
        return {'state': 'pass_' + tab, 'pass_id': identity, 'tab': tab,
                'character_entry': entry, 'scores': scores}

    @staticmethod
    def character_entry(doc,normalized):
        """[入口证据] 只以角色通行证后缀定位；金色边框证明当前选中，不能凭名称跨本领取。"""
        # [区域约束] 整个标签必须位于左侧卡片列，中心落入但跨出区域的文字不能授权点击。
        entries=[line for line in doc.within((210,180,355,410))
                 if line['text'].endswith('角色通行证') and
                 210 <= line['box'][0] < line['box'][2] <= 355 and
                 180 <= line['box'][1] < line['box'][3] <= 410]
        if len(entries)!=1:return None
        line=entries[0];cy=line['point'][1]
        # [可见性门禁] OCR能猜出暗遮挡下的文字并不代表可点击；统一要求标签框内有足够白字像素。
        # 原生、新入口恢复及旧整页恢复均经过这里，不改变普通通行证的页面或领取判断。
        a,b,c,d=map(round,line['box'])
        label_gray=cv2.cvtColor(normalized[b:d,a:c],cv2.COLOR_BGR2GRAY)
        label_bright_fraction=float((label_gray>=180).mean()) if label_gray.size else 0.
        if label_bright_fraction < .08:return None
        hsv=cv2.cvtColor(normalized,cv2.COLOR_BGR2HSV)
        y1,y2=max(0,round(cy-48)),min(720,round(cy+10))
        def gold(x1,x2):
            patch=hsv[y1:y2,x1:x2]
            return int(((patch[:,:,0]>=15)&(patch[:,:,0]<=40)&(patch[:,:,1]>=50)&(patch[:,:,2]>=170)).sum())
        selected=gold(212,218)>=25 and gold(347,353)>=25
        return {'point':[280,cy-20],'suffix':'角色通行证','optional_name':line['text'][:-5],
                'selected_border':selected,'label_box':line['box'],
                'label_bright_fraction':label_bright_fraction}

    @staticmethod
    def character_page(doc,normalized,entry):
        """[页面证据] 名称可缺失/变更，当前选中入口加PASS及真实控件结构才能识别角色页。"""
        if not entry or not entry['selected_border']:return None
        pass_word=any('PASS' in line['text'].upper() for line in doc.within((380,300,610,440)))
        level=any('LEVEL' in line['text'].upper() for line in doc.within((620,190,730,235)))
        basic=doc.find('基础',(780,190,910,235))
        aggregate=doc.find('全部获得',(910,490,1015,535))
        if pass_word and level and basic and aggregate:return {'tab':'levels'}
        # [角色任务页] DAY仅定位任务页；不读取逐项进度、不判断任务是否完成。
        days=any(re.search(r'(?i)DAY[1-9]\d*',line['text']) for line in doc.within((620,190,1015,235)))
        if pass_word and days and aggregate:return {'tab':'tasks'}
        return None

    def inspect_file(self, path, output):
        image = read_image(path)
        overlay = self.reward_overlay.inspect(image)
        if overlay:
            return overlay
        result = self.inspect(image, windows_ocr(path, output))
        # [角色入口恢复] 普通页识别成功仍可能漏读未选中卡片；只补入口，不改当前身份、页签和领取状态。
        if (self.require_character_entry and result['state'] in ('pass_tasks', 'pass_levels')
                and not result.get('character_entry')):
            from model_ocr_provider import recovery_page_ocr
            alternate = recovery_page_ocr(image)
            if alternate:
                normalized = cv2.resize(image, (1280,720), interpolation=cv2.INTER_AREA)
                entry = self.character_entry(OcrDocument(alternate), normalized)
                if entry:
                    return dict(result, character_entry=entry,
                                character_entry_recovery='medium_detector_and_recognizer')
        # [语义恢复] 返回按钮仅证明可返回，不是通行证身份；原生与Medium都走同一检查器。
        if result['state'] in ('unknown','other_page','page_with_back','page_with_circle_back',
                               'page_with_narrow_back','scene_with_home','pass_panel'):
            from model_ocr_provider import recovery_page_ocr
            alternate = recovery_page_ocr(image)
            if alternate:
                recovered = self.inspect(image, alternate)
                if recovered['state'] in ('main_menu','pass_tasks','pass_levels','reward_overlay'):
                    return dict(recovered, recognition_recovery='medium_detector_and_recognizer')
        return result
