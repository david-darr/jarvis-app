"""Playwright side of the contained computer's line protocol.

This source is passed as the container's Python command. It never runs on the
host. Stdout carries only JSON lines: a reply to each numbered command, and,
while someone watches the live view, `{"event": "frame"}` lines from
Chromium's screencast (pushed as the page changes, at the rate the host sets:
10 a second, 30 while the person has control on this computer).
Commands run one at a time, except watch_start/watch_rate/watch_stop, which are
answered at once so the live view never waits behind a page load.
"""
from core.computer_image import DESKTOP_APPS

SOURCE = r'''
import asyncio, base64, io, json, os, re, sys, time
from pathlib import Path
import xml.etree.ElementTree as ET
from playwright.async_api import async_playwright

PROXY = os.environ['KAIROS_COMPUTER_PROXY']
DESKTOP = os.environ.get('KAIROS_COMPUTER_DESKTOP') == '1'
APPS = DESKTOP_APPS_MAPPING
BROWSER_REFUSAL = "That is the browser window: use the computer's web actions (open, read, click by ref) so its safety checks apply."
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

async def xdo(*args, optional=False):
    proc = await asyncio.create_subprocess_exec('xdotool', *map(str, args),
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    try:
        out, err = await asyncio.wait_for(proc.communicate(), 5)
    except asyncio.TimeoutError:
        proc.kill(); await proc.wait()
        raise ValueError('desktop input timed out')
    if proc.returncode and not optional:
        raise ValueError('could not inspect or control the desktop window')
    return out.decode(errors='replace').strip() if proc.returncode == 0 else ''

def xclass(ident):
    # Noble's xdotool predates getwindowclassname; use the same X11 property.
    import ctypes
    class ClassHint(ctypes.Structure):
        _fields_ = [('name', ctypes.c_void_p), ('cls', ctypes.c_void_p)]
    lib = ctypes.CDLL('libX11.so.6')
    lib.XOpenDisplay.argtypes = [ctypes.c_char_p]; lib.XOpenDisplay.restype = ctypes.c_void_p
    lib.XGetClassHint.argtypes = [ctypes.c_void_p, ctypes.c_ulong, ctypes.POINTER(ClassHint)]
    lib.XFree.argtypes = [ctypes.c_void_p]
    lib.XCloseDisplay.argtypes = [ctypes.c_void_p]
    display = lib.XOpenDisplay(b':99')
    if not display: raise ValueError('the desktop display is unavailable')
    hint = ClassHint()
    try:
        if not lib.XGetClassHint(display, int(ident), hint): return ''
        return ctypes.string_at(hint.cls).decode(errors='replace') if hint.cls else ''
    finally:
        if hint.name: lib.XFree(hint.name)
        if hint.cls: lib.XFree(hint.cls)
        lib.XCloseDisplay(display)

async def window_info(ident):
    if not ident or ident == '0': raise ValueError('could not identify the desktop window')
    cls = await xdo('getwindowclassname', ident, optional=True)
    if not cls: cls = xclass(ident)
    return {'id': ident, 'class': cls, 'title': await xdo('getwindowname', ident, optional=True)}

def point(cmd):
    # These bounds are for the display; web actions use page coordinates.
    x, y = cmd.get('x'), cmd.get('y')
    if not isinstance(x, int) or not isinstance(y, int) or not 0 <= x < 1280 or not 0 <= y < 800:
        raise ValueError('choose a point in the desktop frame')
    return x, y

async def window_at(cmd):
    if cmd.get('x') is not None or cmd.get('y') is not None:
        x, y = point(cmd)
        await xdo('mousemove', '--sync', x, y)
    location = dict(line.split('=', 1) for line in (await xdo('getmouselocation', '--shell')).splitlines() if '=' in line)
    return await window_info(location.get('WINDOW'))

async def focused_window():
    return await window_info(await xdo('getactivewindow'))

def browser_window(info):
    return any(word in info.get('class', '').lower() for word in ('chromium', 'chrome', 'kairos-computer-browser'))

def desktop_capture(jpeg=False):
    from PIL import ImageGrab
    output = io.BytesIO()
    ImageGrab.grab(xdisplay=':99').save(output, 'JPEG' if jpeg else 'PNG', **({'quality': 60} if jpeg else {}))
    return base64.b64encode(output.getvalue()).decode()

async def desktop_input(cmd):
    kind = cmd.get('kind')
    if kind not in ('click', 'type', 'key', 'scroll'): raise ValueError('unknown desktop input')
    if kind == 'click': point(cmd)
    at_point = kind in ('click', 'scroll')
    info = await (window_at(cmd) if at_point else focused_window())
    if not cmd.get('person'):
        if browser_window(info): raise ValueError(BROWSER_REFUSAL)
        if not info.get('class'): raise ValueError('could not identify the desktop window; ask the person to take over')
        if info != cmd.get('expected_window'): raise ValueError('the desktop window changed; inspect it again')
    if kind == 'click': await xdo('click', '1')
    elif kind == 'type':
        text = cmd.get('text') or ''
        if len(text) > 10000: raise ValueError('give at most 10000 characters to type')
        await xdo('type', '--clearmodifiers', '--delay', '0', '--', text)
    elif kind == 'key':
        key = cmd.get('key') or ''
        aliases = {'Control': 'ctrl', 'Meta': 'super', 'Enter': 'Return', 'Backspace': 'BackSpace',
                   'ArrowUp': 'Up', 'ArrowDown': 'Down', 'ArrowLeft': 'Left', 'ArrowRight': 'Right', ' ': 'space'}
        parts = [aliases.get(part, part) for part in key.split('+')]
        if not re.fullmatch(r'[A-Za-z0-9_]+(?:\+[A-Za-z0-9_]+)*', '+'.join(parts)) or len(key) > 80:
            raise ValueError('give one key or key combination')
        # xdotool recognises command names even among key arguments.
        if any(part.lower() in ('exec', 'type', 'click', 'key', 'sleep', 'search', 'windowactivate', 'mousemove') for part in parts):
            raise ValueError('give a key, not a desktop command')
        await xdo('key', '--clearmodifiers', '+'.join(parts))
    else:
        for axis, negative, positive in (('dy', 4, 5), ('dx', 6, 7)):
            delta = int(cmd.get(axis) or 0)
            if delta: await xdo('click', '--repeat', min(30, max(1, (abs(delta) + 99) // 100)), '--delay', 0,
                                positive if delta > 0 else negative)

def seed_libreoffice(home=None, registry='/usr/lib/libreoffice/share/registry'):
    config = (Path(home) if home is not None else Path.home()) / '.config/libreoffice/4/user/registrymodifications.xcu'
    if config.exists(): return
    ns = {'oor': 'http://openoffice.org/2001/registry'}
    version = ''
    for installed in sorted(Path(registry).glob('*.xcd')):
        product = ET.parse(installed).find(".//oor:component-data[@oor:name='Setup'][@oor:package='org.openoffice']/"
            "node[@oor:name='Product']/prop[@oor:name='ooSetupVersion']/value", ns)
        if product is not None and product.text:
            version = product.text.strip()
            if re.fullmatch(r'\d+(?:\.\d+)+', version): break
    else: raise ValueError('could not read the installed LibreOffice version')
    config.parent.mkdir(parents=True, exist_ok=True)
    # Seed only new profiles, so kept profiles retain the person's preferences.
    with config.open('x', encoding='utf-8') as f:
        f.write('<?xml version="1.0" encoding="UTF-8"?>\n'
                '<oor:items xmlns:oor="http://openoffice.org/2001/registry">'
                '<item oor:path="/org.openoffice.Office.Common/Misc">'
                '<prop oor:name="ShowTipOfTheDay" oor:op="fuse"><value>false</value></prop>'
                '<prop oor:name="FirstRun" oor:op="fuse"><value>false</value></prop></item>'
                '<item oor:path="/org.openoffice.Setup/Product">'
                '<prop oor:name="ooSetupLastVersion" oor:op="fuse"><value>%s</value></prop></item>'
                '</oor:items>\n' % version)

def browser_launch_options(desktop):
    options = {'headless': not desktop,
        'args': ['--no-sandbox', '--disable-dev-shm-usage', '--proxy-bypass-list=<-loopback>'],
        'proxy': {'server': PROXY, 'bypass': ''}, 'accept_downloads': False}
    if desktop:
        options['args'] += ['--class=kairos-computer-browser', '--window-position=0,0', '--window-size=1280,800']
        options['no_viewport'] = True
    else:
        options['viewport'] = {'width': 1280, 'height': 800}
    return options

async def start_desktop():
    os.environ['DISPLAY'] = ':99'
    os.environ['XDG_RUNTIME_DIR'] = '/tmp/kairos-runtime'
    os.makedirs('/tmp/kairos-runtime', mode=0o700, exist_ok=True)
    os.makedirs('/profile/Documents', exist_ok=True)
    os.makedirs('/profile/.config', exist_ok=True)
    seed_libreoffice()
    with open('/profile/.config/user-dirs.dirs', 'w') as f:
        f.write('XDG_DOCUMENTS_DIR="$HOME/Documents"\n')
    # Use our own menu and bindings: Openbox's default menu includes a terminal.
    menu = '<openbox_menu xmlns="http://openbox.org/3.4/menu"><menu id="root-menu" label="Agent desktop">'
    for app, label in (('files', 'Files'), ('editor', 'Text editor'), ('pdf', 'PDF viewer'), ('images', 'Image viewer')):
        menu += '<item label="%s"><action name="Execute"><command>%s</command></action></item>' % (label, ' '.join(APPS[app]))
    menu += '<menu id="office" label="LibreOffice">'
    for app in ('writer', 'calc', 'impress'):
        menu += '<item label="%s"><action name="Execute"><command>%s</command></action></item>' % (app.title(), ' '.join(APPS[app]))
    menu += '</menu></menu></openbox_menu>'
    with open('/tmp/kairos-menu.xml', 'w') as f: f.write(menu)
    with open('/tmp/kairos-openbox.xml', 'w') as f:
        f.write('<openbox_config xmlns="http://openbox.org/3.4/rc"><focus><followMouse>no</followMouse></focus>'
                '<menu><file>/tmp/kairos-menu.xml</file></menu><keyboard>'
                '<keybind key="C-Escape"><action name="ShowMenu"><menu>root-menu</menu></action></keybind>'
                '<keybind key="A-Tab"><action name="NextWindow"/></keybind>'
                '<keybind key="A-F4"><action name="Close"/></keybind></keyboard>'
                '<mouse><context name="Root"><mousebind button="Left" action="Press">'
                '<action name="ShowMenu"><menu>root-menu</menu></action></mousebind></context>'
                '<context name="Client"><mousebind button="Left" action="Press"><action name="Focus"/>'
                '<action name="Raise"/></mousebind></context></mouse></openbox_config>')
    display = await asyncio.create_subprocess_exec('Xvfb', ':99', '-screen', '0', '1280x800x24', '-nolisten', 'tcp',
        stdout=sys.stderr, stderr=sys.stderr)
    for _ in range(100):
        if display.returncode is not None: raise ValueError('the desktop display stopped')
        if await xdo('getdisplaygeometry', optional=True): break
        await asyncio.sleep(0.05)
    else: raise ValueError('the desktop display did not start')
    await asyncio.create_subprocess_exec('openbox', '--config-file', '/tmp/kairos-openbox.xml',
        stdout=sys.stderr, stderr=sys.stderr)
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
    if DESKTOP: await start_desktop()
    async with async_playwright() as p:
        context = await p.chromium.launch_persistent_context('/profile', **browser_launch_options(DESKTOP))
        await context.add_init_script(script=DOTS)
        page = context.pages[0] if context.pages else await context.new_page()
        refs = {}
        cast = {'session': None, 'page': None, 'sent': 0.0, 'watching': False, 'gap': 0.1, 'task': None}
        async def capture_loop():
            while cast['watching']:
                emit({'event': 'frame', 'data': await asyncio.to_thread(desktop_capture, True)})
                await asyncio.sleep(cast['gap'])
        def on_frame(params):
            session = cast['session']
            if session is None: return
            asyncio.ensure_future(session.send('Page.screencastFrameAck', {'sessionId': params['sessionId']}))
            now = time.monotonic()
            if now - cast['sent'] >= cast['gap']:
                cast['sent'] = now
                emit({'event': 'frame', 'data': params['data']})
        async def stop_cast():
            task, cast['task'] = cast['task'], None
            if task:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            session, cast['session'] = cast['session'], None
            if session is not None:
                try:
                    await session.send('Page.stopScreencast')
                    await session.detach()
                except Exception:
                    pass
        async def start_cast():
            await stop_cast()
            if DESKTOP:
                cast['task'] = asyncio.create_task(capture_loop())
                return
            session = await context.new_cdp_session(page)
            cast['session'], cast['page'] = session, page
            session.on('Page.screencastFrame', on_frame)
            await session.send('Page.startScreencast', {'format': 'jpeg', 'quality': 60,
                                                        'maxWidth': 1280, 'maxHeight': 800, 'everyNthFrame': 1})
        async def follow_page():
            # A popup or new tab became the page: the screencast follows it.
            if not DESKTOP and cast['watching'] and cast['page'] is not page:
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
            if action in ('desktop_screenshot', 'desktop_input', 'window_at', 'focused_window', 'launch', 'windows'):
                if not DESKTOP: raise ValueError('the agent desktop is off')
                if action == 'window_at': return {'window': await window_at(cmd)}
                if action == 'focused_window': return {'window': await focused_window()}
                if action == 'desktop_input': await desktop_input(cmd)
                elif action == 'launch':
                    app = cmd.get('app')
                    if app not in APPS: raise ValueError('choose files, editor, pdf, images, writer, calc or impress')
                    await asyncio.create_subprocess_exec(*APPS[app], cwd='/profile/Documents', stdout=sys.stderr, stderr=sys.stderr)
                    await asyncio.sleep(0.5)
                elif action == 'windows':
                    result['windows'] = []
                    for ident in (await xdo('search', '--onlyvisible', '--class', '.', optional=True)).splitlines():
                        try:
                            info = await window_info(ident)
                            if not info['class']: continue
                            geometry = await xdo('getwindowgeometry', '--shell', ident)
                            info['geometry'] = {k.lower(): int(v) for k, v in
                                (line.split('=', 1) for line in geometry.splitlines() if '=' in line)
                                if k in ('X', 'Y', 'WIDTH', 'HEIGHT')}
                            result['windows'].append(info)
                        except ValueError: continue
                result.update(url=page.url, title=await page.title(), screenshot=await asyncio.to_thread(desktop_capture))
                return result
            if action == 'snapshot' and DESKTOP:
                return {'url':page.url, 'title':await page.title(), 'screenshot':await asyncio.to_thread(desktop_capture, True)}
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
                cast['watching'] = False
                await stop_cast()
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
                if cmd['action'] == 'watch_start' and not cast['session'] and not cast['task']:
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
        cast['watching'] = False
        await stop_cast()

asyncio.run(main())
'''.replace('DESKTOP_APPS_MAPPING', repr(DESKTOP_APPS))
