"""Learned route runner through GameLens; unknown screens stop with a checkpoint."""
from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

import cv2
import numpy as np

from gamelens.marathon import RaceProgress, UnknownScreen, WAYPOINTS
from gamelens.marathon_control import FastExchange
from gamelens.mcp import GameLensClient

# Calibrated world click points AFTER the corresponding saved map waypoint.
# The selected portrait must match before any quest/menu transaction follows.
TRAINER_HEADS={1:(583,275),2:(467,359),3:(481,377),4:(533,369),
               5:(580,377),6:(592,397),7:(443,322),8:(171,361),
               9:(286,395),10:(311,278)}


def athens_shortcut_leg(origin, destination):
    return (origin,destination) in ((1,6),(8,7))


def receipt_mask(frame):
    crop = frame[174:209, 394:970]
    white = cv2.inRange(crop, np.array([190]*3), np.array([255]*3))
    cyan = cv2.inRange(crop, np.array([140,140,0]), np.array([255,255,150]))
    return white | cyan


def receipt_chapter(frame, assets):
    actual = receipt_mask(frame)
    def digits(mask):
        # Judge the actual old/new chapter fields separately from the shared
        # sentence, whose JPEG pixels can vary over a translucent background.
        return np.concatenate((mask[2:19,380:404].ravel(),mask[2:19,534:570].ravel()))
    scores = sorted((int(np.count_nonzero(digits(actual) != digits(assets[f"receipt{chapter}"]))), chapter)
                    for chapter in range(1,17))
    full_distance=np.count_nonzero(actual!=assets[f"receipt{scores[0][1]}"])
    # A held-out crowded-scene success differs by92 antialiased pixels over
    # the full sentence but exactly matches both chapter fields. The nearest
    # wrong chapter differs by230 sentence pixels and33 field pixels.
    if full_distance>120 or scores[0][0]>20 or scores[1][0]-scores[0][0]<12:
        raise UnknownScreen("Exchange receipt unreadable or ambiguous")
    return scores[0][1]


class RaceController(FastExchange):
    def __init__(self, client, profile, evidence, chapter=1, overlap_portals=False):
        super().__init__(client, profile, evidence)
        self.progress = RaceProgress(chapter)
        self.started_at = None
        self.events = []
        self.overlap_portals = overlap_portals
        self._portal_overlap = None

    def read(self):
        frame=super().read()
        overlap=getattr(self,"_portal_overlap",None)
        if overlap is not None and overlap.phase=="awaiting_receipt":
            overlap.observe_arrival(self,refresh=False)
        return frame

    def run(self, trainer):
        chapter=self.progress.chapter
        self.read()
        if (self.overlap_portals and (chapter,trainer) in ((8,1),(14,8))
                and self.athens_portal_ready()):
            from gamelens.marathon_overlap import PortalExchangeOverlap
            self._portal_overlap=PortalExchangeOverlap()
            self._portal_overlap.before_exchange(self,chapter,trainer)
            # Dismount dismisses the NPC menu. Its HUD button reopens it
            # without a ground click that could interrupt the portal cast.
            self.read()
            category_y=280 if trainer==1 else 226
            if self.portrait_matches(trainer) and not self.label_present(self.assets["category"],90,category_y):
                self.act([{"do":"click","x":540,"y":87}],"Open selected Trainer menu while portal casts")
                self.wait_label("category",90,category_y)
        return super().run(trainer)

    def after_exchange_confirmed(self, chapter, trainer):
        overlap=self._portal_overlap
        if overlap is not None and (chapter,trainer)==(overlap.chapter,overlap.trainer):
            self.events[-1]["portal_math_overlap"]=overlap.after_confirmed_exchange(self,chapter,trainer)
            self.checkpoint("confirmed_and_portaled")
            self._portal_overlap=None

    def checkpoint(self, phase):
        data = {"chapter":self.progress.chapter, "phase":phase,
                "started_at":self.started_at, "events":self.events,
                "last_frame":str((self.evidence/f"frame-{self.count:04}.jpg").resolve())}
        (self.evidence/"progress.json").write_text(json.dumps(data, indent=2))

    def area(self):
        title = cv2.inRange(self.frame[35:54,877:1014], np.array([190]*3), np.array([255]*3))
        scores = sorted((int(np.count_nonzero(title != self.assets[name+"_title"])), name)
                        for name in ("city","suburb","marathon"))
        if scores[0][0] > 8 or scores[1][0]-scores[0][0] < 20:
            raise UnknownScreen("Game area is not verified")
        return scores[0][1]

    def map_matches(self, area):
        actual = self.frame[200:250,300:400]
        expected = self.assets[area+"_map"]
        difference = np.abs(actual.astype(np.int16)-expected.astype(np.int16))
        # The translucent paper can show a few world pixels underneath.
        # Inspected suburb frame: 1.64% outliers, mean1.24; wrong city
        # map: 55.3% outliers, mean16.9. Require near-total paper agreement.
        return difference.mean() <= 2 and np.count_nonzero(difference > 12) / difference.size <= .025

    def wait_for(self, test, seconds, description):
        deadline = time.monotonic()+seconds
        while True:
            self.read()
            if test():
                return
            if time.monotonic() >= deadline:
                raise UnknownScreen(description)
            time.sleep(.2)

    def map_to(self, waypoint, area):
        self.read()
        if self.area() != area:
            raise UnknownScreen("Navigation area changed")
        if self.mount_state()!="mounted":
            raise UnknownScreen("Route requires verified Riding before movement")
        self.origin_gps=self.gps_signature()
        self.act([{"do":"tap","key":"m","ms":50}],"Marathon open map")
        self.wait_for(lambda:self.map_matches(area),3,"Known map did not open")
        x,y=waypoint
        self.act([{"do":"click","x":x,"y":y},{"do":"wait","ms":150},
                  {"do":"tap","key":"m","ms":50}],f"Marathon waypoint {x},{y}")

    def gps_signature(self):
        return cv2.inRange(self.frame[59:74,896:986],np.array([190]*3),np.array([255]*3))

    def gps_matches_trainer(self, trainer):
        #90,-125 differs only12 pixels from90,-129: a loose whole-mask
        # match can click the NPC before the character reaches its waypoint.
        # Prefer the stationary fallback over accepting an ambiguous digit.
        return np.count_nonzero(self.gps_signature()!=self.assets[f"arrival{trainer}"])<=2

    def wait_arrival(self, trainer=None):
        deadline=time.monotonic()+90
        moved=False
        previous=None
        stable_since=None
        endpoint_seen=False
        while True:
            self.read()
            gps=self.gps_signature()
            now=time.monotonic()
            if np.count_nonzero(gps!=self.origin_gps)>2:
                moved=True
            if moved and trainer is not None and self.gps_matches_trainer(trainer):
                if endpoint_seen and previous is not None and np.count_nonzero(gps!=previous)<=2:
                    return
                endpoint_seen=True
            else:
                endpoint_seen=False
            if previous is None or np.count_nonzero(gps!=previous)>2:
                stable_since=now
            elif moved and now-stable_since>=1.2:
                return
            previous=gps
            if now>=deadline:
                raise UnknownScreen("Route did not reach a stationary Trainer")
            time.sleep(.2)

    def portrait_matches(self, trainer):
        return self.name_matches(f"trainer{trainer}")

    def name_matches(self, asset):
        title=cv2.inRange(self.frame[35:55,480:640],np.array([190]*3),np.array([255]*3))
        # Inspected Transporter title has a3-pixel JPEG variant; keep the
        # existing tighter Trainer-name check unchanged.
        return np.count_nonzero(title!=self.assets[asset])<=(6 if asset=="transporter" else 2)

    def transfer_search_visible(self):
        actual=cv2.inRange(self.frame[283:299,827:950],np.array([190]*3),np.array([255]*3))
        # The same inspected Location entry over bright city paving differs
        # by15 JPEG threshold pixels. Event and missing-entry fixtures still
        # fail this bounded whole-word match.
        return np.count_nonzero(actual!=self.assets["transfer_search"])<=20

    def transporter_warp(self):
        self.read()
        if not self.name_matches("transporter"):
            raise UnknownScreen("Transporter is not the selected NPC")
        self.wait_label("transmit",90,226)
        self.act([{"do":"click","x":124,"y":237}],"Transporter Transmit menu")
        self.wait_label("suburb_destination",408,277)
        self.act([{"do":"click","x":515,"y":289},{"do":"wait","ms":150},
                  {"do":"click","x":850,"y":407}],"Transporter Suburbs of Athens")
        self.wait_for(lambda:self.area()=="suburb",12,"Transporter suburb warp incomplete")
        self.wait_mount("mounted")

    def transporter_return(self):
        self.read()
        if self.area()!="city" or not self.portrait_matches(6) or self.mount_state()!="mounted":
            raise UnknownScreen("Transporter return origin is not mounted Trainer6")
        self.origin_gps=self.gps_signature()
        self.act([{"do":"click","x":900,"y":230}],"Open city NPC search")
        self.wait_for(self.transfer_search_visible,3,"Transfer(Location) entry not verified")
        self.act([{"do":"click","x":887,"y":291}],"Navigate Transfer(Location) NPC")
        self.wait_arrival("transporter")
        self.read()
        self.act([{"do":"click","x":477,"y":334}],"Select calibrated Transporter")
        self.wait_for(lambda:self.name_matches("transporter"),12,"Expected Transporter was not selected")
        self.transporter_warp()

    def mount_state(self):
        # Icons shift when another timed buff expires. Search the actual
        # buff row; do not interpret one empty fixed slot as dismounted.
        row=cv2.inRange(self.frame[64:85,731:818],np.array([190]*3),np.array([255]*3))
        for x in range(row.shape[1]-20+1):
            if np.count_nonzero(row[:,x:x+20]!=self.assets["mounted_buff"])<=6:
                return "mounted"
        # A different character may have extra timed buffs while on foot.
        # Use only its explicitly inspected profile row, matching HUD and area.
        # The generic profile has no such asset and keeps its existing guards.
        foot_rows=list(self.assets.get("unmounted_buff_rows", []))
        if "unmounted_buff_row" in self.assets:
            foot_rows.append(self.assets["unmounted_buff_row"])
        if any(np.count_nonzero(row!=foot_row)<=6 for foot_row in foot_rows):
            player=cv2.inRange(self.frame[35:53,93:245],np.array([190]*3),np.array([255]*3))
            if np.count_nonzero(player!=self.assets["player_name"])<=4:
                self.area()
                return "unmounted"
        for x in range(row.shape[1]-20+1):
            if np.count_nonzero(row[:,x:x+20]!=self.assets["xp_buff"])<=6:
                outside=row.copy()
                outside[:,max(0,x-3):min(row.shape[1],x+23)]=0
                if np.count_nonzero(outside)<=5:
                    return "unmounted"
        # XP Orb is temporary. An inspected bare buff row is empty after
        # dismount; require both this character's HUD and a known game area
        # so a missing/blank capture never becomes a foot-state proof.
        if np.count_nonzero(row)==0:
            player=cv2.inRange(self.frame[35:53,93:245],np.array([190]*3),np.array([255]*3))
            if np.count_nonzero(player!=self.assets["player_name"])<=4:
                try:
                    self.area()
                except UnknownScreen:
                    pass
                else:
                    return "unmounted"
        raise UnknownScreen("Mount state is obscured or unknown")

    def athens_portal_ready(self):
        actual=self.frame[748:779,414:436]
        difference=np.abs(actual.astype(np.int16)-self.assets["athens_portal_ready"].astype(np.int16))
        score=cv2.matchTemplate(actual,self.assets["athens_portal_ready"],cv2.TM_CCOEFF_NORMED)[0,0]
        # Inspected ready variants differ with JPEG rendering (mean4.3,
        # max48); global cooldown and cast-darkened icons differ by47-89.
        return bool(np.isfinite(score) and score>=.99 and difference.max()<=60 and difference.mean()<=6)

    def riding_ready(self):
        actual=self.frame[748:779,627:651]
        expected=self.assets["riding_ready"]
        difference=np.abs(actual.astype(np.int16)-expected.astype(np.int16))
        score=cv2.matchTemplate(actual,expected,cv2.TM_CCOEFF_NORMED)[0,0]
        return bool(np.isfinite(score) and score>=.99 and difference.max()<=60 and difference.mean()<=6)

    def wait_mount(self, expected, seconds=12):
        def matches():
            try:
                return self.mount_state()==expected
            except UnknownScreen:
                # Transitional pixels may settle; only observe, never repeat7.
                return False
        self.wait_for(matches,seconds,"Riding state did not settle as "+expected)

    def at_athens_spawn(self):
        # Two inspected150,-150 captures differ by10 thresholded JPEG pixels.
        return (self.area()=="city" and
                np.count_nonzero(self.gps_signature()!=self.assets["athens_spawn_gps"])<=12)

    def portal_to_athens(self):
        self.read()
        if self.area() not in ("city","suburb") or self.mount_state()!="mounted":
            raise UnknownScreen("Athens shortcut requires verified mounted origin")
        # Refuse before changing state if the learned skill is not ready.
        if not self.athens_portal_ready():
            raise UnknownScreen("Athens portal hotbar is unknown or cooling down")
        self.act([{"do":"tap","key":"7","ms":50}],"Dismount for Athens shortcut")
        self.wait_mount("unmounted",10)
        # Riding triggers a global cooldown. Wait for the actual ready icon.
        self.wait_for(self.athens_portal_ready,10,"Portal not ready after dismount")
        self.act([{"do":"tap","key":"2","ms":50}],"Cast learned Athens portal")
        self.wait_for(self.at_athens_spawn,15,"Athens portal did not reach verified spawn")
        # The area title changes before the destination world has loaded.
        # Arrival leaves a server global cooldown. The immediate Riding cast
        # was explicitly refused live; wait2.3s plus the actual ready icon.
        time.sleep(2.3)
        self.read()
        if not self.at_athens_spawn():
            raise UnknownScreen("Athens spawn did not settle")
        self.wait_mount("unmounted",5)
        self.wait_for(self.riding_ready,8,"Riding not ready after portal")
        self.act([{"do":"tap","key":"7","ms":50}],"Restore Riding after Athens portal")
        self.wait_mount("mounted")

    def select_trainer(self, trainer):
        self.read()
        x,y=TRAINER_HEADS[trainer]
        self.act([{"do":"click","x":x,"y":y}],f"Marathon select Trainer{trainer}")
        self.wait_for(lambda:self.portrait_matches(trainer),12,"Selected NPC was not expected Trainer")
        if trainer == 10:
            # This waypoint selects Trainer10 and approaches him without
            # opening his dialogue. Use the inspected selected-NPC Menu.
            self.read()
            if not self.label_present(self.assets["category"],90,226):
                self.act([{"do":"click","x":540,"y":87}],"Open selected Trainer10 menu")
                self.wait_label("category",90,226)

    def transition(self, destination_area):
        current=self.area()
        if current==destination_area:
            return
        if {current,destination_area}!={"city","suburb"}:
            raise UnknownScreen("Area transition route has not been verified")
        waypoint=(459,580) if current=="suburb" else (547,587)
        self.map_to(waypoint,current)
        if current=="suburb":
            self.wait_arrival()
            # Verified96,-231 arrival; portal is visible beyond the arch.
            self.act([{"do":"click","x":677,"y":459}],"Enter calibrated Athens portal")
        self.wait_for(lambda:self.area()==destination_area,60,"Area portal transition incomplete")

    def wait_receipt(self, chapter):
        def matches():
            try:
                actual=receipt_chapter(self.frame,self.assets)
            except UnknownScreen:
                return False
            if actual!=chapter:
                raise UnknownScreen("Receipt belongs to a different chapter")
            return True
        self.wait_for(matches,5,"Actual exchange receipt not verified")

    def finish_visible(self):
        actual=cv2.inRange(self.frame[174:193,405:733],np.array([190]*3),np.array([255]*3))
        # Actual race06 finish varies by13 JPEG pixels in this long prefix.
        # The reward/time still require independent final receipt review.
        return np.count_nonzero(actual!=self.assets["finish"])<=20

    def start_claim_visible(self):
        actual=receipt_mask(self.frame)
        expected=self.assets["start_receipt"]
        # Match the actual sentence, excluding blank translucent background,
        # and require the Manual1 digit separately. Held-out JPEG differs33
        # pixels in the sentence but0 in this exact chapter field.
        sentence=np.count_nonzero(actual[:19,11:386]!=expected[:19,11:386])
        chapter=np.count_nonzero(actual[2:19,268:283]!=expected[2:19,268:283])
        return sentence<=45 and chapter<=2

    def daily_limit_visible(self):
        actual=cv2.inRange(self.frame[174:209,405:942],np.array([190]*3),np.array([255]*3))
        return np.count_nonzero(actual!=self.assets["daily_limit"])<=40

    def wait_start_confirmation(self):
        def ready():
            if self.daily_limit_visible():
                self.checkpoint("daily_limit_reached_no_start")
                raise UnknownScreen("Daily Marathon limit reached; no Start sent")
            return self.label_present(self.assets["start"],408,277)
        self.wait_for(ready,12,"Start confirmation or known daily limit not recognized")

    def start_from_confirmation(self):
        self.read()
        if not self.portrait_matches(1) or self.area()!="suburb":
            raise UnknownScreen("Race start is not at Fitness1 in suburb")
        if not self.label_present(self.assets["start"],408,277):
            raise UnknownScreen("Inspected Start confirmation not visible")
        self.started_at=time.time()
        self.act([{"do":"click","x":435,"y":289},{"do":"wait","ms":150},
                  {"do":"click","x":850,"y":407}],"Marathon timed start")
        time.sleep(.8)
        self.read()
        self.checkpoint("start_sent_needs_review")

    def begin_race(self):
        self.read()
        if self.daily_limit_visible():
            self.checkpoint("daily_limit_reached_no_start")
            raise UnknownScreen("Daily Marathon limit reached; no Start sent")
        if self.area()!="suburb":
            raise UnknownScreen("Fresh start must be at Fitness1 in suburb")
        if self.mount_state()!="mounted":
            raise UnknownScreen("Fresh race requires verified Riding")
        if self.finish_visible():
            self.act([{"do":"click","x":959,"y":163}],"Close inspected previous finish")
        self.select_trainer(1)
        self.wait_label("category",90,280)
        self.act([{"do":"click","x":173,"y":291}],"Marathon start category")
        self.wait_label("join",408,277)
        self.act([{"do":"click","x":515,"y":289},{"do":"wait","ms":150},
                  {"do":"click","x":850,"y":407}],"Marathon join")
        self.wait_start_confirmation()
        self.start_from_confirmation()
        self.wait_for(self.start_claim_visible,5,"New Manual1 start was not accepted")
        self.checkpoint("started")
        self.act([{"do":"click","x":959,"y":163}],"Close verified new Manual1 receipt")
        self.run_route(1)

    def run_route(self, current_trainer=1):
        try:
            while not self.progress.complete:
                chapter=self.progress.chapter
                trainer=self.progress.trainer
                self.checkpoint("navigation")
                self.read()
                navigation_started=time.monotonic()
                destination_area="suburb" if trainer<=5 else "city"
                used_portal=bool(athens_shortcut_leg(current_trainer,trainer) and
                                 not self.at_athens_spawn() and self.athens_portal_ready())
                if used_portal:
                    self.portal_to_athens()
                used_transporter=current_trainer==6 and trainer==1
                if used_transporter:
                    self.transporter_return()
                self.transition(destination_area)
                for waypoint in self.progress.navigation(current_trainer,destination_area):
                    self.map_to(waypoint,destination_area)
                    self.wait_arrival(trainer if waypoint==WAYPOINTS[trainer] else None)
                self.select_trainer(trainer)
                navigation_seconds=time.monotonic()-navigation_started
                self.checkpoint("exchange")
                result=self.run(trainer)
                result.update(navigation_seconds=navigation_seconds,athens_portal=used_portal,
                              suburb_transporter=used_transporter)
                if chapter==17:
                    self.wait_for(self.finish_visible,5,"Race finish receipt missing")
                    self.events.append({"chapter":chapter,**result,"finish_wall_seconds":
                                        None if self.started_at is None else time.time()-self.started_at})
                    self.checkpoint("finish_receipt_needs_review")
                    return
                self.wait_receipt(chapter)
                self.progress.exchange_confirmed(chapter,chapter+1)
                self.events.append({"chapter":chapter,**result})
                self.checkpoint("confirmed")
                self.act([{"do":"click","x":959,"y":163}],"Marathon close verified receipt")
                after_exchange=getattr(self,"after_exchange_confirmed",None)
                if after_exchange is not None:
                    after_exchange(chapter,trainer)
                current_trainer=trainer
        except Exception as exc:
            self.checkpoint("stopped: "+str(exc))
            raise

    def resume_equation(self):
        chapter=self.progress.chapter
        trainer=self.progress.trainer
        self.read()
        if not self.portrait_matches(trainer):
            raise UnknownScreen("Equation is not at expected Trainer")
        try:
            result=self.solve_current()
            self.wait_receipt(chapter)
            self.progress.exchange_confirmed(chapter,chapter+1)
            self.events.append({"chapter":chapter,**result})
            self.checkpoint("confirmed")
            self.act([{"do":"click","x":959,"y":163}],"Close resumed verified receipt")
            self.run_route(trainer)
        except Exception as exc:
            self.checkpoint("stopped: "+str(exc))
            raise

    def resume_receipt(self):
        chapter=self.progress.chapter
        trainer=self.progress.trainer
        self.read()
        if not self.portrait_matches(trainer):
            raise UnknownScreen("Receipt not at expected Trainer")
        self.wait_receipt(chapter)
        self.progress.exchange_confirmed(chapter,chapter+1)
        self.events.append({"chapter":chapter,"receipt":str((self.evidence/f"frame-{self.count:04}.jpg").resolve()),
                            "resumed_verified_receipt":True})
        self.checkpoint("confirmed")
        self.act([{"do":"click","x":959,"y":163}],"Close resumed verified receipt")
        self.run_route(trainer)


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument("--evidence-dir",type=Path,required=True)
    parser.add_argument("--chapter",type=int,choices=range(1,18),default=1)
    parser.add_argument("--current-trainer",type=int,choices=range(1,11),default=1)
    parser.add_argument("--started-at",type=float)
    mode=parser.add_mutually_exclusive_group()
    mode.add_argument("--start-confirmation",action="store_true")
    mode.add_argument("--start",action="store_true")
    mode.add_argument("--resume-equation",action="store_true")
    mode.add_argument("--resume-receipt",action="store_true")
    parser.add_argument("--profile",type=Path,default=Path("profiles/godsarena"))
    parser.add_argument("--overlap-portals",action="store_true",help="opt into ready portal casting during exchanges8/14; both shortcut exchanges verified live")
    args=parser.parse_args()
    controller=RaceController(GameLensClient("http://127.0.0.1:8777",None),args.profile,args.evidence_dir,args.chapter,args.overlap_portals)
    controller.started_at=args.started_at
    if args.start_confirmation:
        controller.start_from_confirmation()
        print((args.evidence_dir/"progress.json").read_text())
        return
    if args.start:
        controller.begin_race()
    elif args.resume_equation:
        controller.resume_equation()
    elif args.resume_receipt:
        controller.resume_receipt()
    else:
        controller.run_route(args.current_trainer)
    print((args.evidence_dir/"progress.json").read_text())


if __name__=="__main__":
    main()
