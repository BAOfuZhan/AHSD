import json
import time
import argparse
import os
import logging
import datetime
from zoneinfo import ZoneInfo

# 统一日志时间为北京时间，方便在 GitHub Actions 日志中查看
# 精确到毫秒，格式示例：2026-01-22 19:16:59.123 [Asia/Shanghai] - INFO - ...
class BeijingFormatter(logging.Formatter):
    def formatTime(self, record, datefmt=None):
        """始终将日志时间格式化为北京时间。"""
        dt = datetime.datetime.fromtimestamp(record.created, ZoneInfo("Asia/Shanghai"))
        if datefmt:
            return dt.strftime(datefmt)
        return dt.isoformat()


_formatter = BeijingFormatter(
    fmt="%(asctime)s.%(msecs)03d [Asia/Shanghai] - %(levelname)s - %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
_handler = logging.StreamHandler()
_handler.setFormatter(_formatter)

logging.basicConfig(level=logging.INFO, handlers=[_handler])


def _beijing_now() -> datetime.datetime:
    """获取北京时间（带时区信息）。"""
    return datetime.datetime.now(ZoneInfo("Asia/Shanghai"))


from utils import reserve, get_user_credentials


def _now(action: bool) -> datetime.datetime:
    """获取当前逻辑时间。

    为了在 GitHub Actions 日志中时间统一可读：
    - 本地模式(action=False): 使用本地系统时间；1111
    - GitHub Actions(action=True): 使用北京时间(Asia/Shanghai)。
    """
    if action:
        return _beijing_now()
    return datetime.datetime.now()


# 日志时间：保留 3 位毫秒，和日志头部保持一致
get_log_time = lambda action: _now(action).strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
# 逻辑比较时间：只用到当天的时分秒
get_hms = lambda action: _now(action).strftime("%H:%M:%S")
get_current_dayofweek = lambda action: _now(action).strftime("%A")


SLEEPTIME = 0.1  # 每次抢座的间隔（减少到0.05秒以加快速度）
ENDTIME = "17:00:40"  # 根据学校的预约座位时间+1min即可

ENABLE_SLIDER = False  # 是否有滑块验证（调试阶段先关闭）
ENABLE_TEXTCLICK = False  # 是否有选字验证码（需要图灵云打码平台）
MAX_ATTEMPT = 30  # 最大尝试次数（减少到30次，确保3个配置都能尝试）
RESERVE_NEXT_DAY = True  # 预约明天而不是今天的


# 是否在每一轮主循环中都重新登录。
# True：每一轮都会重新创建会话并登录（原有行为）；
# False：每个账号只在第一次需要时登录一次，后续循环复用同一个会话。
RELOGIN_EVERY_LOOP = True

# 策略相关参数的默认值（可在 config.json 中覆盖）
# STRATEGY_LOGIN_LEAD_SECONDS: 在目标时间前多少秒开始进行登录和基础 session/token 预热
STRATEGY_LOGIN_LEAD_SECONDS = 18
# STRATEGY_SLIDER_LEAD_SECONDS: 在目标时间前多少秒开始进行验证
STRATEGY_SLIDER_LEAD_SECONDS = 14
# FIRST_SUBMIT_OFFSET_MS: 第一次提交时，在目标时间之后再延迟多少毫秒去获取 token 并立即提交
FIRST_SUBMIT_OFFSET_MS = 5
# TARGET_OFFSET2_MS / TARGET_OFFSET3_MS:
# 在第一次失败后，再额外延迟多少毫秒提交第二 / 第三次带验证码的请求
# 例如：1200ms、1500ms
TARGET_OFFSET2_MS = 15
TARGET_OFFSET3_MS = 160


def _get_beijing_target_from_endtime() -> datetime.datetime:
    """根据 ENDTIME 计算目标时间（北京时间，当天 ENDTIME 减 40 秒）。"""
    today = _beijing_now().date()
    h, m, s = map(int, ENDTIME.split(":"))
    end_dt = datetime.datetime(
        year=today.year,
        month=today.month,
        day=today.day,
        hour=h,
        minute=m,
        second=s,
        tzinfo=ZoneInfo("Asia/Shanghai"),
    )
    return end_dt - datetime.timedelta(seconds=40)
    # return end_dt - datetime.timedelta(minutes=1)  # ENDTIME 前 1 分钟（60秒）

def strategic_first_attempt(
    users,
    usernames: str | None,
    passwords: str | None,
    action: bool,
    target_dt: datetime.datetime,
    success_list=None,
):
    """只在第一次调用时使用的“有策略抢座”。

    - 在目标时间前 2 分钟左右开始（由 Actions 的 cron 控制）；
    - 目标时间前 20 秒：预先获取页面 token / algorithm value；
    - 目标时间前 12 秒：预先完成滑块并拿到 validate；
    - 目标时间到达瞬间：直接调用 get_submit 提交一次；
    - 之后的重试逻辑仍交给原有 while 循环和 login_and_reserve。
    """
    if success_list is None:
        success_list = [False] * len(users)

    now = _beijing_now()
    # 如果已经过了目标时间，直接退回到普通逻辑由外层处理
    if now >= target_dt:
        return success_list

    # 等到“目标时间前若干秒”附近再开始策略流程，由 cron 提前少量时间启动
    thirty_before = target_dt - datetime.timedelta(seconds=STRATEGY_LOGIN_LEAD_SECONDS)
    while _beijing_now() < thirty_before:
        time.sleep(0.5)

    usernames_list, passwords_list = None, None
    if action:
        if not usernames or not passwords:
            raise Exception("USERNAMES or PASSWORDS not configured correctly in env")
        usernames_list = usernames.split(",")
        passwords_list = passwords.split(",")
        if len(usernames_list) != len(passwords_list):
            raise Exception("USERNAMES and PASSWORDS count mismatch")

    current_dayofweek = get_current_dayofweek(action)

    for index, user in enumerate(users):
        # 已经成功的配置不再参与策略尝试
        if success_list[index]:
            continue

        username = user["username"]
        password = user["password"]
        times = user["times"]
        roomid = user["roomid"]
        seatid = user["seatid"]
        seat_page_id = user.get("seatPageId")
        fid_enc = user.get("fidEnc")
        daysofweek = user["daysofweek"]

        # 今天不预约该配置，跳过
        if current_dayofweek not in daysofweek:
            logging.info("[strategic] Today not set to reserve, skip this config")
            continue

        # Actions 模式：根据索引或单账号覆盖用户名和密码
        if action:
            if len(usernames_list) == 1:
                username = usernames_list[0]
                password = passwords_list[0]
            elif index < len(usernames_list):
                username = usernames_list[index]
                password = passwords_list[index]
            else:
                logging.error(
                    "[strategic] Index out of range for USERNAMES/PASSWORDS, skipping this config."
                )
                continue

        # seatid 可能是字符串或列表，只在策略阶段针对第一个座位做一次精准尝试
        seat_list = [seatid] if isinstance(seatid, str) else seatid
        if not seat_list:
            logging.error("[strategic] Empty seat list, skip this config")
            continue

        logging.info(
            f"[strategic] Start first attempt for {username} -- {times} -- {seat_list} -- seatPageId={seat_page_id} -- fidEnc={fid_enc}"
        )

        # 1. 在 [T-30s, T] 区间内完成登录和基础 session（不提前获取页面 token）
        s = reserve(
            sleep_time=SLEEPTIME,
            max_attempt=MAX_ATTEMPT,
            enable_slider=ENABLE_SLIDER,
            enable_textclick=ENABLE_TEXTCLICK,
            reserve_next_day=RESERVE_NEXT_DAY,
        )
        s.get_login_status()
        s.login(username, password)
        s.requests.headers.update({"Host": "office.chaoxing.com"})

        first_seat = seat_list[0]

        # 2. 等到“目标时间前若干秒”，预热滑块验证码，提前拿到多份 validate（如果启用了滑块）
        ten_before = target_dt - datetime.timedelta(seconds=STRATEGY_SLIDER_LEAD_SECONDS)
        while _beijing_now() < ten_before:
            time.sleep(0.1)

        captcha1 = captcha2 = captcha3 = ""
        # 根据开关决定是否预热验证码
        if ENABLE_SLIDER:
            # 滑块验证：预先获取三份 validate
            captcha1 = s.resolve_captcha("slide")
            if not captcha1:
                logging.warning(
                    "[strategic] First slider captcha failed or empty, retrying once more"
                )
                captcha1 = s.resolve_captcha("slide")
            logging.info(f"[strategic] Pre-resolved slider captcha1: {captcha1}")

            captcha2 = s.resolve_captcha("slide")
            if not captcha2:
                logging.warning(
                    "[strategic] Second slider captcha failed or empty, retrying once more"
                )
                captcha2 = s.resolve_captcha("slide")
            logging.info(f"[strategic] Pre-resolved slider captcha2: {captcha2}")

            captcha3 = s.resolve_captcha("slide")
            if not captcha3:
                logging.warning(
                    "[strategic] Third slider captcha failed or empty, retrying once more"
                )
                captcha3 = s.resolve_captcha("slide")
            logging.info(f"[strategic] Pre-resolved slider captcha3: {captcha3}")
        elif ENABLE_TEXTCLICK:
            # 选字验证：预先获取三份 validate（循环重试直到成功）
            def get_textclick_with_retry(name: str, max_retries: int = 10) -> str:
                for i in range(max_retries):
                    captcha = s.resolve_captcha("textclick")
                    if captcha:
                        logging.info(f"[strategic] {name} textclick captcha resolved: {captcha}")
                        return captcha
                    logging.warning(f"[strategic] {name} textclick captcha failed, retrying ({i + 1}/{max_retries})")
                    time.sleep(0.5)
                logging.error(f"[strategic] {name} textclick captcha failed after {max_retries} retries")
                return ""

            captcha1 = get_textclick_with_retry("First")
            captcha2 = get_textclick_with_retry("Second")

        # 3. 第一次提交：在目标时间 + FIRST_SUBMIT_OFFSET_MS 毫秒时获取页面 token，获取后立即提交
        token_fetch_dt1 = target_dt + datetime.timedelta(milliseconds=FIRST_SUBMIT_OFFSET_MS)
        while _beijing_now() < token_fetch_dt1:
            # 更短的 sleep 间隔，提高 FIRST_SUBMIT_OFFSET_MS 附近的精度
            time.sleep(0.001)

        logging.info(
            f"[strategic] Fetch page token for first submit at {token_fetch_dt1} (target_dt + {FIRST_SUBMIT_OFFSET_MS}ms)"
        )
        token1, value1 = s._get_page_token(
            s.url.format(
                roomId=roomid,
                day=str(_beijing_now().date()),
                seatPageId=seat_page_id or "",
                fidEnc=fid_enc or "",
            ),
            require_value=True,
        )
        if not token1:
            logging.error("[strategic] Failed to get page token for first submit, skip this config")
            continue
        logging.info(f"[strategic] Got page token for first submit: {token1}, value: {value1}")

        logging.info(
            f"[strategic] Immediately do first submit after fetching page token (target_dt + {FIRST_SUBMIT_OFFSET_MS}ms)"
        )
        suc = s.get_submit(
            url=s.submit_url,
            times=times,
            token=token1,
            roomid=roomid,
            seatid=first_seat,
            captcha=captcha1,
            action=action,
            value=value1,
        )

        # 如果第一次没有成功：为第二次提交重新获取页面 token，再延迟 TARGET_OFFSET2_MS 毫秒提交
        if not suc:
            logging.info("[strategic] First submit failed, prepare second submit with NEW page token")

            # 先重新获取一次页面 token
            token2, value2 = s._get_page_token(
                s.url.format(
                    roomId=roomid,
                    day=str(_beijing_now().date()),
                    seatPageId=seat_page_id or "",
                    fidEnc=fid_enc or "",
                ),
                require_value=True,
            )
            if not token2:
                logging.error("[strategic] Failed to get page token for second submit, skip to third/normal flow")
            else:
                send_dt2 = _beijing_now() + datetime.timedelta(milliseconds=TARGET_OFFSET2_MS)
                while _beijing_now() < send_dt2:
                    time.sleep(0.02)

                logging.info(
                    f"[strategic] Second submit at {send_dt2} (now + {TARGET_OFFSET2_MS}ms) with NEW page token"
                )
                suc = s.get_submit(
                    url=s.submit_url,
                    times=times,
                    token=token2,
                    roomid=roomid,
                    seatid=first_seat,
                    captcha=captcha2,
                    action=action,
                    value=value2,
                )

        # 如果第二次仍未成功：为第三次提交再次获取新的 token，再延迟 TARGET_OFFSET3_MS 毫秒提交
        if not suc:
            logging.info("[strategic] Second submit failed, prepare third submit with NEW page token")

            token3, value3 = s._get_page_token(
                s.url.format(
                    roomId=roomid,
                    day=str(_beijing_now().date()),
                    seatPageId=seat_page_id or "",
                    fidEnc=fid_enc or "",
                ),
                require_value=True,
            )
            if not token3:
                logging.error("[strategic] Failed to get page token for third submit, give up strategic submits for this config")
            else:
                send_dt3 = _beijing_now() + datetime.timedelta(milliseconds=TARGET_OFFSET3_MS)
                while _beijing_now() < send_dt3:
                    time.sleep(0.02)

                logging.info(
                    f"[strategic] Third submit at {send_dt3} (now + {TARGET_OFFSET3_MS}ms) with NEW page token"
                )
                suc = s.get_submit(
                    url=s.submit_url,
                    times=times,
                    token=token3,
                    roomid=roomid,
                    seatid=first_seat,
                    captcha=captcha3,
                    action=action,
                    value=value3,
                )

        success_list[index] = suc

    return success_list


def login_and_reserve(
    users, usernames, passwords, action, success_list=None, sessions=None
):
    logging.info(
        f"Global settings: \nSLEEPTIME: {SLEEPTIME}\nENDTIME: {ENDTIME}\nENABLE_SLIDER: {ENABLE_SLIDER}\nENABLE_TEXTCLICK: {ENABLE_TEXTCLICK}\nRESERVE_NEXT_DAY: {RESERVE_NEXT_DAY}"
    )

    usernames_list, passwords_list = None, None
    if action:
        if not usernames or not passwords:
            raise Exception("USERNAMES or PASSWORDS not configured correctly in env")
        usernames_list = usernames.split(",")
        passwords_list = passwords.split(",")
        if len(usernames_list) != len(passwords_list):
            raise Exception("USERNAMES and PASSWORDS count mismatch")

    if success_list is None:
        success_list = [False] * len(users)

    # 如果传入了 sessions，但长度和 users 不匹配，则忽略 sessions，退回每轮重登
    if sessions is not None and len(sessions) != len(users):
        logging.error("sessions length mismatch with users, ignore sessions and relogin each loop.")
        sessions = None

    current_dayofweek = get_current_dayofweek(action)
    for index, user in enumerate(users):
        username = user["username"]
        password = user["password"]
        times = user["times"]
        roomid = user["roomid"]
        seatid = user["seatid"]
        seat_page_id = user.get("seatPageId")
        fid_enc = user.get("fidEnc")
        daysofweek = user["daysofweek"]

        # 如果今天不在该配置的 daysofweek 中，直接跳过
        if current_dayofweek not in daysofweek:
            logging.info("Today not set to reserve")
            continue

        if action:
            if len(usernames_list) == 1:
                # 只有一个账号，所有配置都用这个账号
                username = usernames_list[0]
                password = passwords_list[0]
            elif index < len(usernames_list):
                username = usernames_list[index]
                password = passwords_list[index]
            else:
                logging.error(
                    "Index out of range for USERNAMES/PASSWORDS, skipping this config."
                )
                continue

        if not success_list[index]:
            logging.info(
                f"----------- {username} -- {times} -- {seatid} try -----------"
            )

            # 根据 RELOGIN_EVERY_LOOP 决定是否复用会话
            s = None
            if sessions is not None:
                s = sessions[index]
                if s is None:
                    # 该账号第一次使用：创建会话并登录
                    s = reserve(
                        sleep_time=SLEEPTIME,
                        max_attempt=MAX_ATTEMPT,
                        enable_slider=ENABLE_SLIDER,
                        enable_textclick=ENABLE_TEXTCLICK,
                        reserve_next_day=RESERVE_NEXT_DAY,
                    )
                    s.get_login_status()
                    s.login(username, password)
                    s.requests.headers.update({"Host": "office.chaoxing.com"})
                    sessions[index] = s
                else:
                    # 复用已有会话，确保 Host 头正确
                    s.requests.headers.update({"Host": "office.chaoxing.com"})
            else:
                # 维持原有行为：每一轮循环都重新创建会话并登录
                s = reserve(
                    sleep_time=SLEEPTIME,
                    max_attempt=MAX_ATTEMPT,
                    enable_slider=ENABLE_SLIDER,
                    enable_textclick=ENABLE_TEXTCLICK,
                    reserve_next_day=RESERVE_NEXT_DAY,
                )
                s.get_login_status()
                s.login(username, password)
                s.requests.headers.update({"Host": "office.chaoxing.com"})

            # 在 GitHub Actions 中传入 ENDTIME，确保内部循环在超过结束时间后及时停止
            suc = s.submit(
                times,
                roomid,
                seatid,
                action,
                ENDTIME if action else None,
                fidEnc=fid_enc,
                seat_page_id=seat_page_id,
            )
            success_list[index] = suc
    return success_list


def main(users, action=False):
    target_dt = _get_beijing_target_from_endtime()
    logging.info(
        f"start time {get_log_time(action)}, action {'on' if action else 'off'}, target_dt {target_dt}"
    )
    attempt_times = 0
    usernames, passwords = None, None
    if action:
        usernames, passwords = get_user_credentials(action)
    success_list = None

    # 根据 RELOGIN_EVERY_LOOP 决定是否为每个用户维护持久会话
    sessions = None
    if not RELOGIN_EVERY_LOOP:
        sessions = [None] * len(users)

    current_dayofweek = get_current_dayofweek(action)
    today_reservation_num = sum(
        1 for d in users if current_dayofweek in d.get("daysofweek")
    )

    # 只在 GitHub Actions 模式下执行一次“有策略”的第一次尝试
    strategic_done = False

    while True:
        # 使用逻辑时间 _now(action)，在 GitHub Actions 下就是北京时间
        current_time = get_hms(action)
        if current_time >= ENDTIME:
            logging.info(
                f"Current time {current_time} >= ENDTIME {ENDTIME}, stop main loop"
            )
            return

        attempt_times += 1

        if not strategic_done and action:
            success_list = strategic_first_attempt(
                users, usernames, passwords, action, target_dt, success_list
            )
            strategic_done = True
        else:
            # 后续尝试使用原有逻辑
            # try:
            success_list = login_and_reserve(
                users, usernames, passwords, action, success_list, sessions
            )
            # except Exception as e:
            #     print(f"An error occurred: {e}")

        print(
            f"attempt time {attempt_times}, time now {current_time}, success list {success_list}"
        )
        if sum(success_list) == today_reservation_num:
            print(f"reserved successfully!")
            return


def debug(users, action=False):
    logging.info(
        f"Global settings: \nSLEEPTIME: {SLEEPTIME}\nENDTIME: {ENDTIME}\nENABLE_SLIDER: {ENABLE_SLIDER}\nENABLE_TEXTCLICK: {ENABLE_TEXTCLICK}\nRESERVE_NEXT_DAY: {RESERVE_NEXT_DAY}"
    )
    suc = False
    logging.info(f" Debug Mode start! , action {'on' if action else 'off'}")

    usernames_list, passwords_list = None, None
    if action:
        usernames, passwords = get_user_credentials(action)
        if not usernames or not passwords:
            logging.error("USERNAMES or PASSWORDS not configured correctly in env.")
            return
        usernames_list = usernames.split(",")
        passwords_list = passwords.split(",")
        if len(usernames_list) != len(passwords_list):
            logging.error("USERNAMES and PASSWORDS count mismatch.")
            return

    current_dayofweek = get_current_dayofweek(action)
    for index, user in enumerate(users):
        username = user["username"]
        password = user["password"]
        times = user["times"]
        roomid = user["roomid"]
        seatid = user["seatid"]
        seat_page_id = user.get("seatPageId")
        fid_enc = user.get("fidEnc")
        daysofweek = user["daysofweek"]
        if type(seatid) == str:
            seatid = [seatid]

        # 如果今天不在该配置的 daysofweek 中，直接跳过，不处理账号
        if current_dayofweek not in daysofweek:
            logging.info("Today not set to reserve")
            continue

        # 在 GitHub Actions 中，从环境变量获取账号密码
        if action:
            if len(usernames_list) == 1:
                # 只有一个账号时，所有配置都用这个账号
                username = usernames_list[0]
                password = passwords_list[0]
            elif index < len(usernames_list):
                username = usernames_list[index]
                password = passwords_list[index]
            else:
                logging.error(
                    "Index out of range for USERNAMES/PASSWORDS, skipping this config."
                )
                continue

        logging.info(f"----------- {username} -- {times} -- {seatid} try -----------")
        s = reserve(
            sleep_time=SLEEPTIME,
            max_attempt=MAX_ATTEMPT,
            enable_slider=ENABLE_SLIDER,
            enable_textclick=ENABLE_TEXTCLICK,
            reserve_next_day=RESERVE_NEXT_DAY,
        )
        s.get_login_status()
        s.login(username, password)
        s.requests.headers.update({"Host": "office.chaoxing.com"})
        suc = s.submit(times, roomid, seatid, action, None, fidEnc=fid_enc, seat_page_id=seat_page_id)
        if suc:
            return


def get_roomid(args1, args2):
    username = input("请输入用户名：")
    password = input("请输入密码：")
    s = reserve(
        sleep_time=SLEEPTIME,
        max_attempt=MAX_ATTEMPT,
        enable_slider=ENABLE_SLIDER,
        enable_textclick=ENABLE_TEXTCLICK,
        reserve_next_day=RESERVE_NEXT_DAY,
    )
    s.get_login_status()
    s.login(username=username, password=password)
    s.requests.headers.update({"Host": "office.chaoxing.com"})
    encode = input("请输入deptldEnc：")
    s.roomid(encode)


if __name__ == "__main__":
    config_path = os.path.join(os.path.dirname(__file__), "config.json")
    parser = argparse.ArgumentParser(prog="Chao Xing seat auto reserve")
    parser.add_argument("-u", "--user", default=config_path, help="user config file")
    parser.add_argument(
        "-m",
        "--method",
        default="reserve",
        choices=["reserve", "debug", "room"],
        help="for debug",
    )
    parser.add_argument(
        "-a",
        "--action",
        action="store_true",
        help="use --action to enable in github action",
    )
    args = parser.parse_args()
    func_dict = {"reserve": main, "debug": debug, "room": get_roomid}
    with open(args.user, "r+") as data:
        config = json.load(data)
        usersdata = config["reserve"]

        # 从 config.json 中读取策略相关配置（如果存在），覆盖默认值
        strategy_cfg = config.get("strategy", {})
        STRATEGY_LOGIN_LEAD_SECONDS = int(
            strategy_cfg.get("login_lead_seconds", STRATEGY_LOGIN_LEAD_SECONDS)
        )
        STRATEGY_SLIDER_LEAD_SECONDS = int(
            strategy_cfg.get("slider_lead_seconds", STRATEGY_SLIDER_LEAD_SECONDS)
        )

        # 控制是否在每一轮主循环中都重新登录
        RELOGIN_EVERY_LOOP = bool(config.get("relogin_every_loop", RELOGIN_EVERY_LOOP))

    func_dict[args.method](usersdata, args.action)
