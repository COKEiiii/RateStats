"""Preview, confirm and send the four workbooks from a completed run."""
import argparse
import sys
from pathlib import Path

from market_rates.common import load
from market_rates.report_email import DELIVERY_MODES, ROOT, SPLIT_PARTS, read_config, send_mail, verified_batch

STATUS = {
    'accepted': 'SMTP 服务器已接受邮件。',
    'partial': 'SMTP 服务器仅接受了部分收件人。',
    'failed': '邮件发送失败。',
    'unknown': '无法确认服务器是否接受邮件。请先检查收件情况，再决定是否补发。',
    'cancelled': '已取消邮件发送。',
    'disabled': '邮件尚未启用，可打开“邮件设置.command”。',
    'busy': '该批次正在发送，请稍后再试。',
    'already_accepted': '已有 SMTP 接受记录，本次跳过。',
    'needs_review': '请核对收件情况后再单独补发。',
}
SMTP_PHASES = {
    'connect': '连接 SMTP 服务器',
    'authenticate': '登录 SMTP',
    'envelope': '提交发件人和收件人',
    'data_command': '等待服务器允许上传',
    'data_upload': '上传邮件和附件',
    'data_reply': '等待服务器最终回执',
    'submit': '提交邮件',
}


def latest_workflow():
    pointer = ROOT / 'outputs/latest-all.json'
    return load(pointer)['workflow'] if pointer.is_file() else None


def confirmation_text(preview, duplicate):
    if duplicate:
        return '同一邮件已有发送或结果未确认的记录。请核对收件情况，确认是否补发。\n\n' + preview
    return preview


def terminal_confirm(preview, duplicate):
    print(confirmation_text(preview, duplicate))
    return input('输入 YES 确认发送，直接回车取消：').strip() == 'YES'


def gui_confirm(root, preview, duplicate):
    from tkinter import messagebox
    return messagebox.askyesno('确认补发' if duplicate else '发送结果邮件',
                               confirmation_text(preview, duplicate), parent=root, default='no')


def show_result(result):
    if result['status'] == 'preview':
        return result['preview']
    if 'parts' in result:
        return '\n\n'.join(show_result(part) for part in result['parts'])
    lines = [STATUS[result['status']]]
    if result.get('part'):
        lines.insert(0, SPLIT_PARTS[result['part']][0])
        if result['status'] in {'needs_review', 'partial', 'unknown'}:
            lines.append('核对收件情况后可单独处理：--split ' + result['part'])
    if result.get('rejected'):
        lines.append('拒收：' + ', '.join(f'{key} ({code})' for key, code in result['rejected'].items()))
    if result.get('error'):
        lines.append('错误类别：' + result['error'])
        phase = result.get('phase')
        if phase in SMTP_PHASES:
            lines.append('出错阶段：' + SMTP_PHASES[phase])
        if result.get('transport_error'):
            lines.append('连接错误：' + result['transport_error'])
        if 'elapsed_seconds' in result:
            lines.append(f"SMTP 耗时：{result['elapsed_seconds']:.1f} 秒")
    if result.get('smtp_code'):
        lines.append(f"SMTP 响应码：{result['smtp_code']}")
    if result.get('record'):
        lines.append('发送记录：' + result['record'])
    return '\n'.join(lines)


def offer_after_completion(workflow, *, gui=False):
    """Called after the workflow has committed; mail never changes its status."""
    config = read_config()
    if not config['enabled']:
        print(STATUS['disabled'], flush=True)
        return
    if not gui and not sys.stdin.isatty():
        print('当前为非交互运行，跳过邮件发送。', flush=True)
        return
    batch = verified_batch(workflow)
    if gui:
        from scripts.email_settings import tk_root
        root = tk_root()
        try:
            result = send_mail(config, batch, lambda *args: gui_confirm(root, *args))
            notify_gui(root, result)
        finally:
            root.destroy()
    else:
        result = send_mail(config, batch, terminal_confirm)
    print(show_result(result), flush=True)


def notify_gui(root, result):
    from tkinter import messagebox
    if result['status'] == 'cancelled':
        return
    dialog = messagebox.showinfo if result['status'] == 'accepted' else messagebox.showwarning
    dialog('发送结果邮件', show_result(result), parent=root)


def choose_and_send():
    from scripts.email_send_gui import choose_and_send as open_send_window

    return open_send_window()


def error_text(exc):
    # Malformed JSON and arbitrary file contents may contain credentials.
    from json import JSONDecodeError
    if type(exc) is ValueError:
        return str(exc)
    if isinstance(exc, JSONDecodeError):
        return '配置或批次记录不是有效的 JSON。'
    return f'无法准备邮件，请检查配置和批次记录（{type(exc).__name__}）。'


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--workflow', help='完整批次目录或 workflow.json')
    parser.add_argument('--dry-run', action='store_true')
    parser.add_argument('--test-to', metavar='EMAIL', help='只向此地址发送测试邮件，清空抄送')
    parser.add_argument('--split', choices=(*DELIVERY_MODES, *SPLIT_PARTS),
                        help='临时覆盖已保存的发送方式，或单独补发全量表或格式对齐表')
    parser.add_argument('--no-cc', action='store_true', help='本次发送抄送留空')
    parser.add_argument('--gui', action='store_true', help='打开独立发送窗口')
    args = parser.parse_args(argv)
    if args.gui:
        if args.workflow or args.dry_run or args.test_to is not None or args.split or args.no_cc:
            parser.error('--gui 必须单独使用')
        return choose_and_send()
    try:
        config = read_config()
        if not (config['enabled'] or args.test_to is not None or args.dry_run):
            print(STATUS['disabled'])
            return 0
        workflow = args.workflow or latest_workflow()
        if not workflow:
            raise ValueError('没有可用的完整批次，请先运行全量流程。')
        if not (args.dry_run or args.test_to is not None or sys.stdin.isatty()):
            raise ValueError('正式发送需要交互确认，请在终端运行或双击“发送结果邮件.command”。')
        confirm = (lambda preview, duplicate: not duplicate) if args.test_to is not None and not sys.stdin.isatty() else terminal_confirm
        result = send_mail(config, verified_batch(workflow), confirm,
                           test_to=args.test_to, dry_run=args.dry_run, split=args.split, no_cc=args.no_cc)
        print(show_result(result))
        return int(result['status'] in {'failed', 'partial', 'unknown', 'busy'} or
                   (result['status'] == 'cancelled' and args.test_to is not None))
    except (ValueError, OSError, KeyError, TypeError, EOFError) as exc:
        print(error_text(exc), file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
