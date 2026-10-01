import json
from pathlib import Path

import numpy as np
import pytest

from gamelens.marathon import MathReader, RaceProgress, UnknownScreen, WAYPOINTS
from gamelens.marathon_control import FastExchange
from gamelens.marathon_race import receipt_chapter, RaceController, athens_shortcut_leg


def test_route_advances_only_with_matching_receipts():
    race = RaceProgress()
    visited = []
    for chapter in range(1, 18):
        visited.append(race.trainer)
        if chapter < 17:
            race.exchange_confirmed(chapter, chapter + 1)
    assert visited == [3, 4, 5, 3, 2, 4, 3, 1, 6, 7, 9, 10, 9, 8, 7, 6, 1]
    with pytest.raises(UnknownScreen):
        race.exchange_confirmed(17, 18)
    race.finish_confirmed(final_manual=17, reward=1200, seconds=4513.097)
    with pytest.raises(UnknownScreen):
        race.finish_confirmed(final_manual=17, reward=1200, seconds=4513.097)


def test_wrong_receipt_never_advances():
    race = RaceProgress()
    for old, new in [(2, 3), (1, 3), (0, 1)]:
        with pytest.raises(UnknownScreen):
            race.exchange_confirmed(old, new)
        assert race.chapter == 1


def test_only_route_legs_near_athens_spawn_use_portal():
    assert athens_shortcut_leg(1,6)
    assert athens_shortcut_leg(8,7)
    for origin,destination in [(1,3),(2,4),(7,9),(9,10),(10,9),(6,1)]:
        assert not athens_shortcut_leg(origin,destination)


def test_arrival_requires_two_consecutive_endpoint_observations(tmp_path,monkeypatch):
    assets=np.load("profiles/godsarena/screens.npz")
    frames=[]
    for trainer in (2,3,2,3,3):
        frame=np.zeros((800,1026,3),np.uint8)
        frame[59:74,896:986]=assets[f"arrival{trainer}"][:,:,None]
        frames.append(frame)

    class MovingClient(FakeClient):
        def tool_see(self,args):
            self.frame=frames.pop(0)
            return super().tool_see(args)

    client=MovingClient(frames[0])
    controller=RaceController(client,Path("profiles/godsarena"),tmp_path)
    controller.origin_gps=assets["arrival1"]
    monkeypatch.setattr("gamelens.marathon_race.time.sleep",lambda seconds:None)
    controller.wait_arrival(3)
    assert frames==[]
    assert controller.count==5
    assert client.actions==[]


def test_transition_and_stalled_route_are_explicit():
    race = RaceProgress(chapter=6)
    assert race.navigation(2, "suburb") == [(600,451), WAYPOINTS[4]]
    race = RaceProgress(chapter=9)
    with pytest.raises(UnknownScreen):
        race.navigation(1, "suburb")
    assert race.navigation(1, "city") == [WAYPOINTS[6]]


def render_equation(text, bank):
    frame = np.zeros((800, 1026, 3), dtype=np.uint8)
    x = 416
    for char in text:
        glyph = np.asarray(bank[char][0], dtype=bool)
        h, w = glyph.shape
        frame[283:283+h, x:x+w][glyph] = 255
        x += w + 3
    return frame


def test_game_font_equations_and_unknown_operator():
    bank = json.loads(Path("profiles/godsarena/math-glyphs.json").read_text())
    reader = MathReader(bank)
    for equation, answer in [("80+47=", 127), ("68+34=", 102), ("15+15=", 30), ("58+57=", 115)]:
        assert reader.read(render_equation(equation, bank)) == (equation, answer)
    changed = render_equation("15+15=", bank)
    # Erase operator's vertical stroke: must not accept minus as plus.
    changed[283:293, 436:448] = 0
    with pytest.raises(UnknownScreen):
        reader.read(changed)
    with pytest.raises(UnknownScreen):
        reader.read(np.zeros((800, 1026, 3), dtype=np.uint8))
    with pytest.raises(UnknownScreen):
        reader.read(np.zeros((600, 800, 3), dtype=np.uint8))


def test_untrained_live_font_position_variation():
    import cv2
    reader=MathReader.load(Path("profiles/godsarena/math-glyphs.json"))
    frame=np.zeros((800,1026,3),np.uint8)
    frame[282:298,414:510]=cv2.imread("tests/fixtures/godsarena/75-plus-47.png")
    # The first7 at this text position was not added to the supervised bank.
    assert reader.read(frame)==("75+47=",122)
    frame[282:298,414:510]=cv2.imread("tests/fixtures/godsarena/53-plus-56.png")
    assert reader.read(frame)==("53+56=",109)
    frame[282:298,414:510]=cv2.imread("tests/fixtures/godsarena/26-plus-91.png")
    assert reader.read(frame)==("26+91=",117)


class FakeClient:
    def __init__(self, frame, refused=False):
        self.frame = frame
        self.refused = refused
        self.actions = []

    def tool_state(self, args):
        return [{"type": "text", "text": json.dumps({
            "target": {"hwnd": 1, "width": 1026, "height": 800, "foreground": True},
            "capture": {"healthy": True, "session_id": 1},
        })}], False

    def tool_see(self, args):
        import base64
        import cv2
        raw = cv2.imencode(".png", self.frame)[1].tobytes()
        return [{"type": "image", "data": base64.b64encode(raw).decode()}], False

    def tool_act(self, args):
        self.actions.append(args)
        return [{"type": "text", "text": "act " + json.dumps({
            "outcome": "denied" if self.refused else "sent",
        })}], self.refused


def test_wrong_selected_npc_causes_no_input(tmp_path):
    client = FakeClient(np.zeros((800, 1026, 3), dtype=np.uint8))
    controller = FastExchange(client, Path("profiles/godsarena"), tmp_path)
    with pytest.raises(UnknownScreen, match="expected Trainer"):
        controller.run(3)
    assert client.actions == []


def test_refused_action_stops_without_retry(tmp_path):
    profile = Path("profiles/godsarena")
    assets = np.load(profile / "screens.npz")
    frame = np.zeros((800, 1026, 3), dtype=np.uint8)
    frame[35:55, 480:640] = assets["trainer3"][:, :, None]
    h, w = assets["category"].shape[:2]
    frame[228:228+h, 90:90+w] = assets["category"]
    client = FakeClient(frame, refused=True)
    controller = FastExchange(client, profile, tmp_path)
    with pytest.raises(UnknownScreen, match="no retry"):
        controller.run(3)
    assert len(client.actions) == 1
    assert client.actions[0]["strict"] is True


def test_receipt_bank_distinguishes_every_learned_chapter():
    assets = np.load("profiles/godsarena/screens.npz")
    for chapter in range(1,17):
        # Recreate the mask as white pixels. This exercises ambiguity ranking
        # across all receipts, including adjacent two-digit chapter numbers.
        frame = np.zeros((800,1026,3),np.uint8)
        frame[174:209,394:970] = assets[f"receipt{chapter}"][:,:,None]
        assert receipt_chapter(frame,assets)==chapter
    with pytest.raises(UnknownScreen):
        receipt_chapter(np.zeros((800,1026,3),np.uint8),assets)
    import cv2
    frame=np.zeros((800,1026,3),np.uint8)
    frame[174:209,394:970]=cv2.imread("tests/fixtures/godsarena/manual4-to-5.png")
    assert receipt_chapter(frame,assets)==4
    frame[174:209,394:970]=cv2.imread("tests/fixtures/godsarena/manual1-to-2.png")
    assert receipt_chapter(frame,assets)==1
    frame[174:193,405:650]=0
    with pytest.raises(UnknownScreen):
        receipt_chapter(frame,assets)


def test_other_npc_titles_do_not_pass_as_expected_trainer(tmp_path):
    assets=np.load("profiles/godsarena/screens.npz")
    frame=np.zeros((800,1026,3),np.uint8)
    client=FakeClient(frame)
    controller=RaceController(client,Path("profiles/godsarena"),tmp_path)
    for actual in range(1,11):
        frame[35:55,480:640]=assets[f"trainer{actual}"][:,:,None]
        controller.frame=frame
        for expected in range(1,11):
            assert controller.portrait_matches(expected)==(expected==actual)
        assert not controller.name_matches("transporter")


def test_wrong_npc_cannot_open_transporter_destination(tmp_path):
    assets=np.load("profiles/godsarena/screens.npz")
    frame=np.zeros((800,1026,3),np.uint8)
    frame[35:55,480:640]=assets["trainer6"][:,:,None]
    client=FakeClient(frame)
    controller=RaceController(client,Path("profiles/godsarena"),tmp_path)
    with pytest.raises(UnknownScreen,match="Transporter is not"):
        controller.transporter_warp()
    assert client.actions==[]


def test_transfer_search_bright_background_and_wrong_entry(tmp_path):
    import cv2
    frame=np.zeros((800,1026,3),np.uint8)
    controller=RaceController(FakeClient(frame),Path("profiles/godsarena"),tmp_path)
    controller.frame=frame
    frame[283:299,827:950]=cv2.imread("tests/fixtures/godsarena/search-transfer-location.png")
    assert controller.transfer_search_visible()
    frame[283:299,827:950]=cv2.imread("tests/fixtures/godsarena/search-transfer-event.png")
    assert not controller.transfer_search_visible()
    frame[:]=0
    assert not controller.transfer_search_visible()


def test_near_endpoint_different_digit_cannot_trigger_fast_arrival(tmp_path):
    import cv2
    assets=np.load("profiles/godsarena/screens.npz")
    frame=np.zeros((800,1026,3),np.uint8)
    controller=RaceController(FakeClient(frame),Path("profiles/godsarena"),tmp_path)
    controller.frame=frame
    frame[59:74,896:986]=cv2.imread("tests/fixtures/godsarena/gps-near-trainer1.png")
    assert not controller.gps_matches_trainer(1)
    frame[59:74,896:986]=assets["arrival1"][:,:,None]
    assert controller.gps_matches_trainer(1)


def test_start_claim_requires_manual1_digit(tmp_path):
    import cv2
    frame=np.zeros((800,1026,3),np.uint8)
    frame[174:209,394:970]=cv2.imread("tests/fixtures/godsarena/start-manual1.png")
    controller=RaceController(FakeClient(frame),Path("profiles/godsarena"),tmp_path)
    controller.frame=frame
    assert controller.start_claim_visible()
    frame[176:193,662:677]=0
    assert not controller.start_claim_visible()
    frame[:]=0
    assert not controller.start_claim_visible()


def test_portal_flag_checkpoint_is_json_serializable(tmp_path):
    assets=np.load("profiles/godsarena/screens.npz")
    frame=np.zeros((800,1026,3),np.uint8)
    frame[748:779,414:436]=assets["athens_portal_ready"]
    controller=RaceController(FakeClient(frame),Path("profiles/godsarena"),tmp_path)
    controller.read()
    ready=controller.athens_portal_ready()
    assert ready is True
    controller.events=[{"chapter":9,"athens_portal":ready}]
    controller.checkpoint("confirmed")
    assert json.loads((tmp_path/"progress.json").read_text())["events"][0]["athens_portal"] is True


def test_mount_buff_shift_and_obscured_state(tmp_path):
    assets=np.load("profiles/godsarena/screens.npz")
    frame=np.zeros((800,1026,3),np.uint8)
    controller=RaceController(FakeClient(frame),Path("profiles/godsarena"),tmp_path)
    controller.frame=frame
    # A timed buff expiring shifts the Riding icon by a full slot.
    for x in (738,768):
        frame[:]=0
        frame[64:85,x:x+20]=assets["mounted_buff"][:,:,None]
        frame[64:85,796:816]=assets["xp_buff"][:,:,None]
        assert controller.mount_state()=="mounted"
    frame[:]=0
    frame[64:85,796:816]=assets["xp_buff"][:,:,None]
    assert controller.mount_state()=="unmounted"
    # Missing HUD or an unrelated visible icon must not imply foot mode.
    frame[:]=0
    with pytest.raises(UnknownScreen,match="Mount state"):
        controller.mount_state()
    frame[64:85,796:816]=assets["xp_buff"][:,:,None]
    frame[64:85,740:750]=255
    with pytest.raises(UnknownScreen,match="Mount state"):
        controller.mount_state()


def test_portal_not_ready_and_unmounted_route_send_no_input(tmp_path):
    assets=np.load("profiles/godsarena/screens.npz")
    frame=np.zeros((800,1026,3),np.uint8)
    frame[35:54,877:1014]=assets["city_title"][:,:,None]
    frame[64:85,768:788]=assets["mounted_buff"][:,:,None]
    client=FakeClient(frame)
    controller=RaceController(client,Path("profiles/godsarena"),tmp_path)
    with pytest.raises(UnknownScreen,match="cooling down"):
        controller.portal_to_athens()
    assert client.actions==[]
    frame[64:85,731:818]=0
    frame[64:85,796:816]=assets["xp_buff"][:,:,None]
    with pytest.raises(UnknownScreen,match="verified Riding"):
        controller.map_to(WAYPOINTS[7],"city")
    assert client.actions==[]


def test_real_ready_cooldown_and_spawn_jpeg_variants(tmp_path):
    import cv2
    assets=np.load("profiles/godsarena/screens.npz")
    frame=np.zeros((800,1026,3),np.uint8)
    controller=RaceController(FakeClient(frame),Path("profiles/godsarena"),tmp_path)
    controller.frame=frame
    for name,expected in [("portal-ready",True),("portal-global-cooldown",False),("portal-casting",False)]:
        frame[748:779,414:436]=cv2.imread(f"tests/fixtures/godsarena/{name}.png")
        assert controller.athens_portal_ready()==expected
    frame[35:54,877:1014]=assets["city_title"][:,:,None]
    frame[59:74,896:986]=cv2.imread("tests/fixtures/godsarena/spawn-athens.png")
    assert controller.at_athens_spawn()
    frame[35:54,877:1014]=assets["suburb_title"][:,:,None]
    assert not controller.at_athens_spawn()
    frame[35:54,877:1014]=assets["city_title"][:,:,None]
    frame[59:74,896:986]=0
    assert not controller.at_athens_spawn()


def test_finish_prefix_jpeg_variant_and_nonfinish_receipt(tmp_path):
    import cv2
    frame=np.zeros((800,1026,3),np.uint8)
    controller=RaceController(FakeClient(frame),Path("profiles/godsarena"),tmp_path)
    controller.frame=frame
    frame[174:193,405:733]=cv2.imread("tests/fixtures/godsarena/finish-race06.png")
    assert controller.finish_visible()
    frame[:]=0
    frame[174:209,394:970]=cv2.imread("tests/fixtures/godsarena/manual1-to-2.png")
    assert not controller.finish_visible()


def test_daily_limit_stops_before_any_start_input(tmp_path):
    import cv2
    frame=np.zeros((800,1026,3),np.uint8)
    frame[174:209,405:942]=cv2.imread("tests/fixtures/godsarena/daily-limit.png")
    client=FakeClient(frame)
    controller=RaceController(client,Path("profiles/godsarena"),tmp_path)
    with pytest.raises(UnknownScreen,match="Daily Marathon limit"):
        controller.begin_race()
    assert client.actions==[]
    assert controller.started_at is None
    assert json.loads((tmp_path/"progress.json").read_text())["phase"]=="daily_limit_reached_no_start"
    with pytest.raises(UnknownScreen,match="Daily Marathon limit"):
        controller.wait_start_confirmation()
    assert client.actions==[]
    frame[:]=0
    frame[174:209,394:970]=cv2.imread("tests/fixtures/godsarena/manual1-to-2.png")
    controller.frame=frame
    assert not controller.daily_limit_visible()


def test_unmounted_without_xp_requires_bare_row_and_known_hud(tmp_path):
    import cv2
    assets=np.load("profiles/godsarena/screens.npz")
    frame=np.zeros((800,1026,3),np.uint8)
    frame[64:85,731:818]=cv2.imread("tests/fixtures/godsarena/no-xp-foot-row.png")
    frame[35:53,93:245]=assets["player_name"][:,:,None]
    frame[35:54,877:1014]=assets["suburb_title"][:,:,None]
    controller=RaceController(FakeClient(frame),Path("profiles/godsarena"),tmp_path)
    controller.frame=frame
    assert controller.mount_state()=="unmounted"
    frame[35:53,93:245]=0
    with pytest.raises(UnknownScreen):
        controller.mount_state()
    frame[35:53,93:245]=assets["player_name"][:,:,None]
    frame[64:85,740:750]=255
    with pytest.raises(UnknownScreen):
        controller.mount_state()
