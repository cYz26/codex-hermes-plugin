#!/bin/bash
notification_recovery_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
notification_recovery_entry="$notification_recovery_dir/refresh_macos_notification.py"
if [[ ! -f "$notification_recovery_entry" ]]; then
    notification_recovery_entry="$notification_recovery_dir/desktop/refresh_macos_notification.py"
fi
python3 "$notification_recovery_entry" --recover-authorization
notification_recovery_status=$?
printf '\n报告位于安装包内的 report/notification-refresh-report.json。\n按回车关闭窗口。\n'
read -r notification_recovery_close
exit "$notification_recovery_status"
