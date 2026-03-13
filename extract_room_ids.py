import argparse
import json
import logging
from typing import Any
from urllib.parse import parse_qs, urlparse

from utils import AES_Decrypt, reserve


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="登录超星后根据 fidEnc 提取 roomId 与房间名"
    )
    parser.add_argument("--username", required=True, help="超星账号")
    parser.add_argument("--password", required=True, help="超星明文密码")
    parser.add_argument(
        "--password-encrypted",
        action="store_true",
        help="password 参数是 AES 密文（与项目一致的加密方式）",
    )
    parser.add_argument("--fid-enc", help="学校的 fidEnc / deptIdEnc")
    parser.add_argument(
        "--seat-url",
        help="选座页面 URL（可自动提取 fidEnc），例如 .../select?id=xxx&seatId=xxx&fidEnc=xxx",
    )
    parser.add_argument("--page-size", type=int, default=200, help="每页数量，默认 200")
    parser.add_argument(
        "--json",
        action="store_true",
        help="以 JSON 格式输出，便于复制到其他系统",
    )
    parser.add_argument(
        "--worker-zones-json",
        action="store_true",
        help="输出 Worker 前端可直接粘贴的 reading_zone_groups JSON",
    )
    return parser.parse_args()


def _extract_fid_enc_from_url(seat_url: str) -> str:
    parsed = urlparse(seat_url)
    qs = parse_qs(parsed.query)
    return (qs.get("fidEnc") or [""])[0].strip()


def _build_room_name(item: dict[str, Any]) -> str:
    parts = [
        (item.get("firstLevelName") or "").strip(),
        (item.get("secondLevelName") or "").strip(),
        (item.get("thirdLevelName") or "").strip(),
    ]
    parts = [p for p in parts if p]
    return "-".join(parts) if parts else (item.get("name") or "未命名房间")


def _build_worker_zone_groups(room_list: list[dict[str, Any]]) -> list[dict[str, Any]]:
    groups: dict[str, list[dict[str, str]]] = {}
    for item in room_list:
        floor = (item.get("firstLevelName") or "未分层").strip() or "未分层"
        second = (item.get("secondLevelName") or "").strip()
        third = (item.get("thirdLevelName") or "").strip()
        zone_name = "-".join([p for p in [second, third] if p]) or _build_room_name(item)
        zone = {
            "id": str(item.get("id", "")),
            "name": zone_name,
        }
        groups.setdefault(floor, []).append(zone)

    # 尽量把“数字楼层”按楼层序排序，其余放后面
    def floor_sort_key(floor_name: str):
        digits = "".join(ch for ch in floor_name if ch.isdigit())
        if digits:
            return (0, int(digits), floor_name)
        return (1, 9999, floor_name)

    return [
        {"floor": floor, "zones": zones}
        for floor, zones in sorted(groups.items(), key=lambda kv: floor_sort_key(kv[0]))
    ]


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s - %(message)s")
    args = parse_args()

    password = AES_Decrypt(args.password) if args.password_encrypted else args.password

    fid_enc = (args.fid_enc or "").strip()
    if not fid_enc and args.seat_url:
        fid_enc = _extract_fid_enc_from_url(args.seat_url)
        if fid_enc:
            logging.info("已从 seat-url 解析 fidEnc: %s", fid_enc)

    if not fid_enc:
        logging.error("缺少 fidEnc：请传 --fid-enc，或传包含 fidEnc 参数的 --seat-url")
        return 1

    s = reserve()
    try:
        # 先访问登录状态接口，初始化会话上下文
        s.get_login_status()
    except Exception:
        pass

    ok, msg = s.login(args.username, password)
    if not ok:
        logging.error("登录失败: %s", msg)
        return 1

    url = (
        "https://office.chaoxing.com/data/apps/seat/room/list"
        f"?cpage=1&pageSize={args.page_size}&firstLevelName=&secondLevelName=&thirdLevelName=&deptIdEnc={fid_enc}"
    )

    resp = s.requests.get(url=url, timeout=20)
    try:
        data = resp.json()
    except Exception:
        logging.error("room/list 返回非 JSON: HTTP %s", resp.status_code)
        print(resp.text[:400])
        return 2

    if not data.get("success"):
        logging.error("room/list 调用失败: %s", data.get("msg") or "unknown")
        return 3

    room_list = (data.get("data") or {}).get("seatRoomList") or []
    if not room_list:
        logging.warning("未提取到房间，请检查 fidEnc 是否正确")
        return 0

    normalized = [
        {
            "roomid": str(item.get("id", "")),
            "name": _build_room_name(item),
            "seatPageId": str(item.get("id", "")),
            "fidEnc": fid_enc,
        }
        for item in room_list
    ]

    worker_zone_groups = _build_worker_zone_groups(room_list)

    if args.worker_zones_json:
        print(json.dumps(worker_zone_groups, ensure_ascii=False, indent=2))
        return 0

    if args.json:
        print(json.dumps(normalized, ensure_ascii=False, indent=2))
    else:
        print("提取结果（可直接用于 config）：")
        for i, item in enumerate(normalized, 1):
            print(f"{i:02d}. {item['name']}  roomid={item['roomid']}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
