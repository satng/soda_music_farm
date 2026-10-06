#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
汽水音乐「看广告领免费听时长」全自动脚本 —— 脱离 AI 独立运行

原理:
  hd-adb 截图 → macOS Vision 框架 OCR(中文) → 文本+坐标 → 状态机决策 → input tap
  (蓝叠 adbd 有命令白名单, 所有 shell 命令需以 "getprop ...; " 拼接绕过)

流程规则(2026-10-06 实测固化):
  1. 启动汽水音乐 → 底部「福利」→ 福利中心 → 右侧「领时长」
  2. 广告右上角倒计时「N秒后可领取奖励」→ 每 20s 截图检测
  3. 广告可能跳直播间落地页 → 按返回键回广告页; 「为你精选以下直播」弹窗点其右上角 X
  4. 倒计时结束 → 「领取成功 X」→ 点 X 关广告
  5. 弹「再看N个视频得xx免费听」→ 点「领取奖励」→ 继续广告
  6. 广告内优惠券弹窗只点小 ×, 永不点「立即使用/跳转微信/查看详情」
  7. 链条结束(只剩「坚持退出」)→ 点它回福利中心, 「领时长」可点则再来一轮

用法:
  ~/..../envs/default/bin/python3 soda_music_farm.py --rounds 20 --shots ./shots
  Ctrl+C 随时停止
"""

import argparse
import subprocess
import sys
import time
from pathlib import Path

import Vision
import Quartz

HDADB = "/Applications/BlueStacks.app/Contents/MacOS/hd-adb"
PKG = "com.luna.music"
WORK = Path(__file__).resolve().parent

# 兜底坐标(1080x1920, 仅当 OCR 找不到对应元素时使用)
FALLBACK = {
    "tab_welfare": (758, 1838),
    "claim_time": (956, 746),
    "reward_claim": (545, 1010),
    "ad_close": (1010, 80),
    "signin_close": (538, 1405),
    "coupon_close": (538, 1481),
    "live_recommend_close": (1021, 937),
}
# 绝不主动点击的文案
FORBIDDEN = ("跳转微信", "立即使用", "点击使用", "查看详情", "立即下载", "退出不看了")


def log(msg):
    print(time.strftime("[%H:%M:%S] ") + msg, flush=True)


# ---------------- adb 层 ----------------
class Adb:
    def __init__(self):
        out = subprocess.run([HDADB, "devices"], capture_output=True,
                             text=True, timeout=15).stdout
        for line in out.splitlines()[1:]:
            parts = line.split()
            if len(parts) >= 2 and parts[1] == "device":
                self.serial = parts[0]
                log(f"设备: {self.serial}")
                return
        sys.exit("未找到模拟器: 请确认蓝叠已启动且开启 ADB 调试")

    def sh(self, cmd, timeout=40):
        r = subprocess.run([HDADB, "-s", self.serial, "shell",
                            f"getprop ro.build.version.sdk; {cmd}"],
                           capture_output=True, text=True, timeout=timeout)
        return r.stdout

    def tap(self, x, y):
        log(f"    ▶ 点击 ({int(x)},{int(y)})")
        self.sh(f"input tap {int(x)} {int(y)}")

    def back(self):
        log("    ▶ 返回键")
        self.sh("input keyevent 4")

    def launch(self):
        self.sh(f"monkey -p {PKG} -c android.intent.category.LAUNCHER 1")


# ---------------- OCR 层 ----------------
def ocr(png_path):
    """返回 [(text, cx, cy), ...] 像素坐标"""
    w, h = _img_size(png_path)
    url = Quartz.CFURLCreateWithFileSystemPath(
        None, str(png_path), Quartz.kCFURLPOSIXPathStyle, False)
    src = Quartz.CGImageSourceCreateWithURL(url, None)
    cg = Quartz.CGImageSourceCreateImageAtIndex(src, 0, None)
    req = Vision.VNRecognizeTextRequest.alloc().init()
    req.setRecognitionLanguages_(["zh-Hans", "en-US"])
    req.setRecognitionLevel_(Vision.VNRequestTextRecognitionLevelAccurate)
    handler = Vision.VNImageRequestHandler.alloc().initWithCGImage_options_(cg, None)
    handler.performRequests_error_([req], None)
    out = []
    for obs in (req.results() or []):
        b = obs.boundingBox()
        cx = (b.origin.x + b.size.width / 2) * w
        cy = (1 - b.origin.y - b.size.height / 2) * h
        out.append((obs.topCandidates_(1)[0].string(), cx, cy))
    return out


def _img_size(p):
    # 不依赖 PIL: PNG 头 IHDR 宽高位于固定偏移
    with open(p, "rb") as f:
        head = f.read(24)
    return int.from_bytes(head[16:20], "big"), int.from_bytes(head[20:24], "big")


class Eye:
    """截图 + OCR, 带文本检索"""
    def __init__(self, adb, shots_dir=None):
        self.adb = adb
        self.dir = Path(shots_dir) if shots_dir else None
        if self.dir:
            self.dir.mkdir(parents=True, exist_ok=True)
        self.n = 0
        self.last_texts = []

    def snapshot(self, tag=""):
        self.n += 1
        png = Path(f"/data/local/tmp/_farm_{self.n}.png")
        self.adb.sh(f"screencap -p {png}; base64 {png}")
        b64 = self.adb.sh(f"base64 {png}")
        lines = b64.splitlines()
        if lines and lines[0].strip().isdigit():
            lines = lines[1:]
        import base64
        local = WORK / f"_snap.png"
        local.write_bytes(base64.b64decode("".join(lines)))
        if self.dir:
            (self.dir / f"{time.strftime('%H%M%S')}_{self.n}_{tag}.png") \
                .write_bytes(local.read_bytes())
        self.last_texts = ocr(local)
        return self.last_texts

    def find(self, *keywords, exclude=(), region=None, max_len=None):
        """返回第一个命中关键词的 (text, x, y); region=(x1,y1,x2,y2) 像素区域"""
        for t, x, y in self.last_texts:
            if any(k in t for k in keywords):
                if any(x_ in t for x_ in exclude):
                    continue
                if max_len is not None and len(t) > max_len:
                    continue
                if region and not (region[0] <= x <= region[2] and region[1] <= y <= region[3]):
                    continue
                return t, x, y
        return None

    def has(self, *keywords, **kw):
        return self.find(*keywords, **kw) is not None


# ---------------- 状态机 ----------------
class Farm:
    def __init__(self, adb, eye, poll=20, idle_limit=75):
        self.adb, self.eye = adb, eye
        self.poll = poll            # 倒计时检测间隔(用户要求 20s)
        self.idle_limit = idle_limit
        self.rep = 0                # 连续相同动作计数(熔断用)
        self.last_sig = None
        self.last_cd = None         # 倒计时卡死检测
        self.cd_rep = 0

    def run(self, rounds):
        self.adb.launch()
        time.sleep(12)
        for rnd in range(1, rounds + 1):
            log(f"—— 第 {rnd} 轮 ——")
            acted = self.cycle()
            if not acted:
                log("连续无动作, 结束")
                break
            time.sleep(3)
        log("全部轮次完成")

    def cycle(self):
        last_act = time.time()
        while time.time() - last_act < self.idle_limit:
            self.eye.snapshot()
            act = self.step()
            if act:
                last_act = time.time()
            else:
                time.sleep(self.poll)
        return True

    def _guard(self, sig, max_rep=6):
        """同一动作连续超过 max_rep 次 → 熔断: 按返回键打破僵局"""
        if sig == self.last_sig:
            self.rep += 1
        else:
            self.rep, self.last_sig = 0, sig
        if self.rep >= max_rep:
            log("  ⚠️ 动作重复过多, 熔断: 按返回键")
            self.rep = 0
            self.adb.back()
            time.sleep(4)
            return True
        return False

    def step(self):
        e, fb = self.eye, FALLBACK
        # —— 弹窗类优先(浮在广告之上, 必须先处理) ——
        hit = e.find("继续互动", "继续观看", "去互动", "浏览领取", max_len=8)
        if hit:
            if self._guard(f"interact:{hit[0]}"):
                return True
            log(f"  🖱️ 广告要求互动/继续观看: {hit[0]}")
            self.adb.tap(hit[1], hit[2])
            time.sleep(8)
            return True

        # 广告即将自动跳转直播间 → 点取消拦住
        if e.has("后进入直播间") and e.find("取消", max_len=4):
            hit = e.find("取消", max_len=4)
            log("  拦截自动跳转直播间 → 点取消")
            self.adb.tap(hit[1], hit[2])
            time.sleep(3)
            return True

        # 奖励弹窗按钮: 限定屏幕中部弹窗区域 + 短文本, 防止顶部广告横幅长串误匹配
        # (必须先于「领取成功」判断: 弹窗浮在广告上时, 顶部的领取成功×点不到)
        hit = e.find("领取奖励", exclude=("成功", "已领", "秒"),
                     region=(300, 880, 800, 1160), max_len=8)
        if hit:
            if self._guard("reward_btn"):
                return True
            log(f"  🎁 奖励弹窗: {hit[0]}")
            self.adb.tap(hit[1], hit[2])
            time.sleep(8)
            return True

        # —— 广告页状态 ——
        # 倒计时: 文本含「可领取奖励」且含「秒」(容忍 OCR 错字, 如"竹秒居可领取奖励")
        hit = e.find("可领取奖励")
        if hit and "秒" in hit[0] and "成功" not in hit[0]:
            log(f"  ⏳ 广告倒计时中: {hit[0]}")
            if hit[0] == self.last_cd:
                self.cd_rep += 1
            else:
                self.cd_rep, self.last_cd = 0, hit[0]
            if self.cd_rep >= 3:   # 倒计时 ~60s 不走 → 卡住, 按返回键恢复
                log("  ⚠️ 倒计时卡住, 按返回键尝试恢复")
                self.cd_rep = 0
                self.adb.back()
                time.sleep(4)
            else:
                time.sleep(self.poll)
            return True

        hit = e.find("领取成功", max_len=10)
        if hit and hit[2] < 200:  # 右上角徽章
            _, x, y = hit
            if self._guard(f"success:{x:.0f}"):
                return True
            log("  ✅ 领取成功 → 关闭广告")
            self.adb.tap(x + 62, y)   # X 紧贴徽章右侧
            time.sleep(4)
            return True

        if e.has("为你精选以下直播", "为你精选"):
            log("  关闭「为你精选」推荐弹窗")
            self.adb.tap(*fb["live_recommend_close"])
            time.sleep(3)
            return True

        if e.has("恭喜获得", "后过期") and e.has("立即使用", "点击使用"):
            log("  关闭广告内优惠券弹窗(不点使用)")
            self.adb.tap(*fb["coupon_close"])
            time.sleep(3)
            return True

        if not e.has("坚持退出") and not e.find(
                "领取奖励", region=(300, 880, 800, 1160), max_len=8) and e.has(
                "进入直播间", "更多直播", "直播间观看时长计入奖励时长", "说点什么"):
            if self._guard("landing_back"):
                return True
            log("  广告落地页(直播间) → 等观看计时后返回")
            time.sleep(15)          # 直播间观看时长计入奖励, 先停留
            self.adb.back()
            time.sleep(3)
            return True

        if e.has("坚持退出") and not e.find(
                "领取奖励", region=(300, 880, 800, 1160), max_len=8):
            if self._guard("quit"):
                return True
            log("  领取链结束 → 点「坚持退出」")
            hit = e.find("坚持退出")
            self.adb.tap(hit[1], hit[2])
            time.sleep(4)
            return True

        if e.has("签到7天领", "每天连续签到"):
            log("  关闭签到弹窗")
            self.adb.tap(*fb["signin_close"])
            time.sleep(3)
            return True

        # —— App 页面 ——
        hit = e.find("领时长")
        if hit:
            log("  点「领时长」")
            self.adb.tap(hit[1], hit[2])
            time.sleep(8)
            return True

        if e.has("免费听时长", "金币收益") and not e.has("领时长"):
            log("  福利中心但暂无可领入口, 等待")
            return False

        if e.has("发现", "听抖音"):
            log("  回主界面 → 点「福利」tab")
            self.adb.tap(*fb["tab_welfare"])
            time.sleep(5)
            return True

        log(f"  未知界面(识别到{len(e.last_texts)}条文本), 稍候重试")
        return False


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--rounds", type=int, default=20, help="最大轮数")
    ap.add_argument("--poll", type=int, default=20, help="倒计时检测间隔秒")
    ap.add_argument("--shots", default="", help="截图归档目录")
    args = ap.parse_args()
    adb = Adb()
    eye = Eye(adb, args.shots or None)
    Farm(adb, eye, poll=args.poll).run(args.rounds)


if __name__ == "__main__":
    main()
