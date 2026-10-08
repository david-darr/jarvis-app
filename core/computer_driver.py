"""Playwright side of the contained computer's line protocol.

This source is passed as the container's Python command. It never runs on the
host. Stdout carries only JSON lines: a reply to each numbered command, and,
while someone watches the live view, `{"event": "frame"}` lines from
Chromium's screencast (pushed as the page changes, at the rate the host sets:
10 a second, 30 while the person has control on this computer).
Commands run one at a time, except watch_start/watch_rate/watch_stop, which are
answered at once so the live view never waits behind a page load.
"""
SOURCE = r'''
import asyncio, base64, json, os, sys, time
from playwright.async_api import async_playwright

PROXY = os.environ['KAIROS_COMPUTER_PROXY']
MAX_FRAME_DEPTH = 5
MAX_FPS = 30
PRIVATE_SELECTOR = 'input[type=password],input[autocomplete=current-password],input[autocomplete=new-password],input[autocomplete=one-time-code],input[autocomplete^=cc-]'
PRIVATE_STYLE = PRIVATE_SELECTOR + ' { visibility: hidden !important; }'
# The live view is a screencast, which screenshot masks don't reach: private
# fields show as dots in every document instead.
DOTS = """(() => { const css = %s; const add = () => { const s = document.createElement('style'); s.textContent = css;
  (document.head || document.documentElement).appendChild(s); };
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', add); else add(); })()""" % json.dumps(
  PRIVATE_SELECTOR + ' { -webkit-text-security: disc !important; }')

def emit(message):
    sys.stdout.write(json.dumps(message) + '\n')
    sys.stdout.flush()
ELEMENT = """e => {if (!e) return {}; const a = e.closest('a,button,input,textarea,select,[role=button],[contenteditable=true]') || e;
const f = a.closest('form');
const signals = n => [n.getAttribute('aria-label'), n.labels?.[0]?.innerText, n.innerText,
  n.matches('button,input[type=submit],input[type=button],input[type=reset],input[type=image],[role=button]') ? n.value : '',
  n.getAttribute('alt'), n.getAttribute('placeholder'), n.getAttribute('title')].map(v => (v || '').trim()).filter(Boolean);
const labels = signals(a);
const submitControls = f ? Array.from(document.querySelectorAll('button:not([type]),button[type=submit],input[type=submit],input[type=image]')).filter(n => n.form === f) : [];
return {tag:a.tagName.toLowerCase(), type:(a.getAttribute('type')||'').toLowerCase(), autocomplete:(a.getAttribute('autocomplete')||'').toLowerCase(), role:a.getAttribute('role')||'', label:(labels[0] || '').slice(0,160),
submit: a.matches('button:not([type]),button[type=submit],input[type=submit],input[type=image]') || !!(f && a.type === 'submit'),
signals:labels, form_submits:submitControls.map(n => ({label:(signals(n)[0] || '').slice(0,160), signals:signals(n)})),
form:!!f, href:a.href||''};}"""

async def main():
    async with async_playwright() as p:
        context = await p.chromium.launch_persistent_context('/profile', headless=True,
            args=['--no-sandbox', '--disable-dev-shm-usage', '--proxy-bypass-list=<-loopback>'],
            proxy={'server': PROXY, 'bypass': ''}, viewport={'width':1280,'height':800}, accept_downloads=False)
        await context.add_init_script(script=DOTS)
        page = context.pages[0] if context.pages else await context.new_page()
        refs = {}
        cast = {'session': None, 'page': None, 'sent': 0.0, 'watching': False, 'gap': 0.1}
        def on_frame(params):
            session = cast['session']
            if session is None: return
            asyncio.ensure_future(session.send('Page.screencastFrameAck', {'sessionId': params['sessionId']}))
            now = time.monotonic()
            if now - cast['sent'] >= cast['gap']:
                cast['sent'] = now
                emit({'event': 'frame', 'data': params['data']})
        async def stop_cast():
            session, cast['session'] = cast['session'], None
            if session is not None:
                try:
                    await session.send('Page.stopScreencast')
                    await session.detach()
                except Exception:
                    pass
        async def start_cast():
            await stop_cast()
            session = await context.new_cdp_session(page)
            cast['session'], cast['page'] = session, page
            session.on('Page.screencastFrame', on_frame)
            await session.send('Page.startScreencast', {'format': 'jpeg', 'quality': 60,
                                                        'maxWidth': 1280, 'maxHeight': 800, 'everyNthFrame': 1})
        async def follow_page():
            # A popup or new tab became the page: the screencast follows it.
            if cast['watching'] and cast['page'] is not page:
                await start_cast()
        async def _frame_child(handle):
            try:
                return await handle.content_frame()
            except Exception:
                return None
        async def element(arg):
            if arg.get('ref'):
                handle = refs.get(str(arg['ref']))
                if not handle: return None, False
                try:
                    tag = await handle.evaluate('(e) => e.tagName.toLowerCase()')
                except Exception:
                    return None, True
                return (None, True) if tag in ('iframe', 'frame') else (handle, False)
            if arg.get('x') is None or arg.get('y') is None:
                return None, False
            frame = page.main_frame
            point = {'x': arg['x'], 'y': arg['y']}
            inside_frame = False
            for depth in range(MAX_FRAME_DEPTH + 1):
                try:
                    hit = await frame.evaluate_handle('p => document.elementFromPoint(p.x,p.y)', point)
                    handle = hit.as_element()
                    if not handle: return None, inside_frame
                    tag = await handle.evaluate('(e) => e.tagName.toLowerCase()')
                    if tag not in ('iframe', 'frame'): return handle, False
                    inside_frame = True
                    if depth == MAX_FRAME_DEPTH: return None, True
                    child = await _frame_child(handle)
                    if not child: return None, True
                    offset = await handle.evaluate('e => { const r = e.getBoundingClientRect(); const s = getComputedStyle(e); return {'
                        'x:r.left + parseFloat(s.borderLeftWidth || 0) + parseFloat(s.paddingLeft || 0),'
                        'y:r.top + parseFloat(s.borderTopWidth || 0) + parseFloat(s.paddingTop || 0)}; }')
                    point = {'x': point['x'] - offset['x'], 'y': point['y'] - offset['y']}
                    frame = child
                except Exception:
                    if inside_frame: return None, True
                    raise
            return None, True
        async def focused():
            frame = page.main_frame
            inside_frame = False
            for depth in range(MAX_FRAME_DEPTH + 1):
                try:
                    hit = await frame.evaluate_handle('document.activeElement')
                    handle = hit.as_element()
                    if not handle: return None, inside_frame
                    tag = await handle.evaluate('(e) => e.tagName.toLowerCase()')
                    if tag not in ('iframe', 'frame'): return handle, False
                    inside_frame = True
                    if depth == MAX_FRAME_DEPTH: return None, True
                    child = await _frame_child(handle)
                    if not child: return None, True
                    frame = child
                except Exception:
                    if inside_frame: return None, True
                    raise
            return None, True
        async def inspect(arg):
            handle, opaque = await element(arg)
            if opaque: return {'opaque_frame': True, 'tag': 'iframe'}
            if not handle: return {}
            try:
                return await handle.evaluate(ELEMENT)
            except Exception:
                return {'opaque_frame': True, 'tag': 'iframe'}
        async def inspect_focused():
            handle, opaque = await focused()
            if opaque: return {'opaque_frame': True, 'tag': 'iframe'}
            if not handle: return {}
            try:
                return await handle.evaluate(ELEMENT)
            except Exception:
                return {'opaque_frame': True, 'tag': 'iframe'}
        async def execute(cmd):
            nonlocal page
            action = cmd['action']
            result = {}
            if action == 'state': return {'url':page.url, 'title':await page.title()}
            if 'expected_url' in cmd and page.url != cmd['expected_url']:
                raise ValueError('the page changed; inspect it again')
            if action == 'inspect': return {'element': await inspect(cmd)}
            if action == 'focused': return {'element': await inspect_focused()}
            if action in ('click','type','key') and 'expected' in cmd:
                current = await (inspect_focused() if action == 'key' or
                                 (action == 'type' and not cmd.get('ref') and cmd.get('x') is None)
                                 else inspect(cmd))
                if current.get('opaque_frame'): raise ValueError('embedded frame needs the person to take over')
                if current != cmd['expected']: raise ValueError('the element changed; inspect again')
                autocomplete = current.get('autocomplete','')
                private = (current.get('type') == 'password' and current.get('tag') == 'input'
                           or autocomplete in ('current-password','new-password','one-time-code')
                           or autocomplete.startswith('cc-'))
                if private and (action == 'type' or action == 'key' and cmd.get('key','').lower() not in ('tab','shift+tab','escape')):
                    raise ValueError('private fields need the person to take over')
            if action == 'person_click': await page.mouse.click(cmd['x'], cmd['y'])
            elif action == 'person_type': await page.keyboard.insert_text(cmd['text'])
            elif action == 'person_key': await page.keyboard.press(cmd['key'])
            elif action == 'person_scroll': await page.mouse.wheel(cmd.get('dx',0), cmd.get('dy',0))
            elif action == 'open': await page.goto(cmd['url'], wait_until='domcontentloaded', timeout=30000)
            elif action == 'click':
                h, opaque = await element(cmd)
                if opaque: raise ValueError('embedded frame needs the person to take over')
                if not h: raise ValueError('no element at that point or ref')
                if cmd.get('x') is not None and cmd.get('y') is not None:
                    await page.mouse.click(cmd['x'], cmd['y'])
                else:
                    await h.click(timeout=10000)
            elif action == 'type':
                h, opaque = await element(cmd)
                if opaque: raise ValueError('embedded frame needs the person to take over')
                if h: await h.fill(cmd['text'])
                else: await page.keyboard.insert_text(cmd['text'])
            elif action == 'key': await page.keyboard.press(cmd['key'])
            elif action == 'scroll': await page.mouse.wheel(cmd.get('dx',0),cmd.get('dy',0))
            elif action == 'back': await page.go_back(wait_until='domcontentloaded', timeout=30000)
            elif action == 'wait': await asyncio.sleep(min(10,max(0,float(cmd.get('seconds',1)))))
            elif action == 'read':
                refs.clear()
                handles = await page.locator('a,button,input,textarea,select,[role=button],[contenteditable=true]').element_handles()
                entries = []
                for h in handles[:100]:
                    if not await h.is_visible(): continue
                    info = await h.evaluate(ELEMENT)
                    ref = str(len(entries)+1); refs[ref] = h
                    entries.append({'ref':ref, **info})
                visible = await page.locator('body').inner_text(timeout=10000)
                result = {'text':visible[:12000] + ('\n... page text cut here' if len(visible)>12000 else ''),
                          'elements':entries[:60]}
            elif action not in ('screenshot','snapshot','done'): raise ValueError('unknown action')
            if action == 'done':
                await context.close(); return {'done':True}
            if action.startswith('person_'):
                if context.pages and context.pages[-1] is not page:
                    page = context.pages[-1]
                    refs.clear()
                    await follow_page()
                return {'url':page.url, 'title':await page.title()}
            await page.wait_for_timeout(250)
            if context.pages and context.pages[-1] is not page:
                page = context.pages[-1]
                await page.wait_for_load_state('domcontentloaded', timeout=10000)
                refs.clear()
                await follow_page()
            private = page.locator(PRIVATE_SELECTOR)
            screenshot_type = 'jpeg' if action == 'snapshot' else 'png'
            quality = {'quality': 65} if action == 'snapshot' else {}
            result.update(url=page.url, title=await page.title(),
                screenshot=base64.b64encode(await page.screenshot(type=screenshot_type, mask=[private],
                    style=PRIVATE_STYLE, **quality)).decode())
            return result
        async def run(cmd):
            try:
                return {'id':cmd['id'], **(await execute(cmd))}
            except Exception as e:
                return {'id':cmd.get('id'), 'error':str(e)}
        async def watch(cmd):
            try:
                if cmd.get('fps'):
                    cast['gap'] = 1 / min(MAX_FPS, max(1, int(cmd['fps'])))
                if cmd['action'] == 'watch_start' and not cast['session']:
                    cast['watching'] = True
                    await start_cast()
                elif cmd['action'] == 'watch_stop':
                    cast['watching'] = False
                    await stop_cast()
                emit({'id':cmd['id'], 'ok':True})
            except Exception as e:
                emit({'id':cmd['id'], 'error':str(e)})
        queue = asyncio.Queue()
        async def worker():
            while (cmd := await queue.get()) is not None:
                reply = await run(cmd)
                emit(reply)
                if reply.get('done'): return
        working = asyncio.create_task(worker())
        while line := await asyncio.to_thread(sys.stdin.readline):
            try:
                cmd = json.loads(line)
            except ValueError:
                emit({'id':None, 'error':'unreadable command'})
                continue
            if cmd.get('action') in ('watch_start', 'watch_rate', 'watch_stop'):
                asyncio.ensure_future(watch(cmd))
            else:
                await queue.put(cmd)
            if working.done(): break
        if not working.done():
            await queue.put(None)
            await working

asyncio.run(main())
'''
