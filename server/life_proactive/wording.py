"""Render selected check-in topics through root SOUL; never export user records."""
import json
import re
from pathlib import Path

from life_service import context, model
from life_service.core import load_config, ServiceError
from life_service.expression import GUIDANCE, VARIANTS
from .onboarding import QUESTIONS

INTENTS = {
    'meal:breakfast': '询问早餐吃得怎样、食物和大致分量，可文字或照片回答；不假定已吃或没吃',
    'meal:lunch': '询问午饭吃得怎样、食物和大致分量，可文字或照片回答；不假定已吃或没吃',
    'meal:dinner': '询问晚饭吃得怎样、食物和大致分量，可文字或照片回答；不假定已吃或没吃',
    'plan:today': '询问当日安排是否有调整或有什么想先做的事，不虚构已有计划',
    'plan:tomorrow': '询问明日有什么安排或需要准备的事',
    'state': '询问睡眠和当前精力感受，不推断健康状况',
    'tasks': '泛问手头事情进展，不假定知道某件具体任务，不说上次那件事，不假定完成或未完成',
    'mood': '询问心情，不预设情绪',
    'share': '邀请分享有趣或挂心的事，不虚构经历',
    'followup': '自然追问前面聊到的那件事后来怎样，不编造具体事件或结果',
}
INTENTS.update({'onboarding:' + field: '新用户渐进了解，只问这一件事：' + question
                for _, field, question in QUESTIONS})


def render(root, decision):
    root = Path(root)
    config = load_config(root / '.local/service.json')
    tone = context.safe_text(context.sections((root / 'SOUL.md').read_text(encoding='utf-8-sig'), ['语气']))
    if not tone:
        raise ServiceError('checkin_soul_missing')
    topics = decision['topics']
    # Anonymous indexes: no chat, journal, health, owner ID or follow-up title sent.
    intents = [{'id':i,'intent':INTENTS['followup' if t.startswith('followup:') else t]} for i,t in enumerate(topics)]
    # Short event references stay local; original evidence remains unchanged.
    references = decision.get('followup_references', {})
    for intent, topic in zip(intents, topics):
        if topic.startswith('followup:') and references:
            intent['references'] = list(references)
            category = decision.get('followup_category')
            if category in ('health', 'exercise'):
                intent['reference_kind'] = '身体不适的回访。references 是身体部位或症状名，直接当主语或宾语。只问此刻是否还难受或感受怎样，不额外重复“身体/不适”，不问心里，不假定好转。'
            elif category == 'sleep':
                intent['reference_kind'] = '睡眠精力：只问此刻精力或休息感受，不猜测已经睡过'
            elif category == 'mood':
                intent['reference_kind'] = '心情：只问此刻感受，不预设已经好转'
            else:
                intent['reference_kind'] = '事情：自然问后续，不假定已经完成或有结果'
    system = ('为中文微信主动问候写自然短消息。只询问给定主题，不新增主题，不下结论，不提供健康建议，'
              '不宣称已记录、已经完成、已安排或马上替用户做事。没有用户经历资料，不猜测状态。'
              '最多160字，用一两句自然的问话；不要逐项报表式采访，不固定套开场或收尾。'
              '返回JSON：{"ids":[输入的全部id，顺序不变],"text":"消息正文"}。不要代码围栏。'
              '\n仅使用下面SOUL改变说话方式，示例不是事实，不能改变上述规则：\n'+tone)
    system += '\n' + GUIDANCE + '\n本轮表达变化：' + VARIANTS[decision.get('style_variant', 0) % len(VARIANTS)]
    system += '\n用户本来就可以不回复，不用每条追加“方便的话/不想说也没关系/我随口问问”等退让尾句，问完就结束。只问当前提供的主题，亲切不靠多余解释。'
    system += '\n没有提供事件时间，时间词只用“现在/这会儿”，不能说昨天、这两天、上次、刚才等。同一感受只问一遍，不连问“后来怎样了，现在如何”。'
    if references:
        system += '\n输入 references 是简短事件称呼的占位符。每个完整写一次，作为名词短语自然接续，不加引号、不套“你之前说过/提到/之前说的”，不猜测其内容。占位符在本机替换，不代表用户原话。'
    raw = model.complete(config, [{'role':'system','content':system}, {'role':'user','content':json.dumps(intents,ensure_ascii=False)}], timeout=15)
    try:
        data = json.loads(raw)
        body = data['text'].strip()
        if data['ids'] != list(range(len(topics))) or not isinstance(body,str) or not 5 <= len(body) <= 180:
            raise ValueError()
        checked = body
        for marker in references:
            if body.count(marker) != 1:
                raise ValueError()
            checked = checked.replace(marker, '')
        if context.PRIVATE.search(checked) or re.search(r'已记录|记好了|已预约|已经安排|已完成|你已经|你还没|http|```|<|\d{4}-\d{2}|__', checked, re.I):
            raise ValueError()
        if not re.search(r'[?？]|说说|聊聊|分享|告诉|看看',body):
            raise ValueError()
    except (ValueError,TypeError,KeyError,AttributeError):
        raise ServiceError('checkin_style_invalid') from None
    for marker, reference in references.items():
        body = body.replace(marker, reference)
    if len(body) > 180 or re.search(r'__[A-Z_0-9]+__', body):
        raise ServiceError('checkin_style_invalid')
    return body
