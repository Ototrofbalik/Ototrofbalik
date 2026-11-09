"""Layout doğrulama + düzenleme testleri."""
import pytest

from backend.demo.demo_factory import build_demo_layout
from backend.layout.editing import add_workstation, clone_layout, move_workstation, remove_workstation
from backend.layout.geometry import terminal_point
from backend.layout.validation import validate_layout
from backend.schemas import Corridor, Obstacle, Point, Rect, Rotation, StationKind, Workstation


def codes(layout):
    return {i.code for i in validate_layout(layout).issues}


def test_demo_layout_is_valid():
    rep = validate_layout(build_demo_layout())
    assert rep.valid, rep.issues
    assert rep.issues == []


def test_out_of_bounds():
    lay = move_workstation(build_demo_layout(), "ship", 28, 2)   # 28+4 > 30
    rep = validate_layout(lay)
    assert not rep.valid
    assert any(i.code == "OUT_OF_BOUNDS" and i.entity_ids == ["ship"] for i in rep.issues)


def test_rotation_changes_footprint_bounds():
    # 3 x 4 ayak izi 90 derece: x yönünde 3 m. x=27 -> 27..30 tamam; rotation 0 -> 27..31 sınır dışı
    lay = move_workstation(build_demo_layout(), "ship", 27, 2, rotation="90")
    assert "OUT_OF_BOUNDS" not in codes(lay)
    lay = move_workstation(build_demo_layout(), "ship", 27, 2, rotation="0")
    assert "OUT_OF_BOUNDS" in codes(lay)


def test_overlap():
    lay = move_workstation(build_demo_layout(), "qc", 4, 14)    # asm (3..7, 14..17) ile çakışır
    rep = validate_layout(lay)
    assert any(i.code == "OVERLAP" and set(i.entity_ids) == {"asm", "qc"} for i in rep.issues)
    assert not rep.valid


def test_safety_gap_not_overlap():
    lay = move_workstation(build_demo_layout(), "ship", 21.3, 2)   # drill x2 = 21 -> 0.3 m < 0.5
    cs = codes(lay)
    assert "SAFETY_GAP" in cs and "OVERLAP" not in cs
    lay = move_workstation(build_demo_layout(), "ship", 21.5, 2)   # tam 0.5 m -> tamam
    assert "SAFETY_GAP" not in codes(lay)


def test_obstacle_overlap():
    lay = move_workstation(build_demo_layout(), "pack", 5, 2)      # kolon (6.5..7.5, 3..4) içinde
    rep = validate_layout(lay)
    assert any(i.code == "OBSTACLE_OVERLAP" and set(i.entity_ids) == {"pack", "col1"} for i in rep.issues)


def test_touching_is_not_overlap():
    # raw (1..5) ile pack'i 5.5'e koy: boşluk 0.5 -> geçerli
    lay = move_workstation(build_demo_layout(), "pack", 5.5, 2)
    assert "OVERLAP" not in codes(lay)


def test_corridor_too_narrow():
    lay = build_demo_layout()
    lay.corridors[1] = lay.corridors[1].model_copy(update={"width": 1.5})
    rep = validate_layout(lay)
    assert any(i.code == "CORRIDOR_TOO_NARROW" and i.entity_ids == ["west"] for i in rep.issues)


def test_corridor_blocked_by_obstacle_and_station():
    lay = build_demo_layout()
    lay.obstacles.append(Obstacle(id="wall1", name="Duvar", rect=Rect(x=14, y=9, w=1, h=2)))
    rep = validate_layout(lay)
    assert any(i.code == "CORRIDOR_BLOCKED" and i.entity_ids == ["main", "wall1"] for i in rep.issues)
    lay2 = add_workstation(build_demo_layout(), Workstation(id="mid", name="Orta", x=14, y=9, width=3, length=2))
    assert any(i.code == "CORRIDOR_BLOCKED" and "mid" in i.entity_ids for i in validate_layout(lay2).issues)


def test_no_corridor_access_far_station():
    ws = Workstation(id="far", name="Uzak", x=13, y=16.5, width=3, length=2, kind=StationKind.MACHINE)
    # ayak izi alt kenarı y=16.5; koridor y=10 -> 6.5 m > 6 m
    rep = validate_layout(add_workstation(build_demo_layout(), ws))
    assert any(i.code == "NO_CORRIDOR_ACCESS" and i.entity_ids == ["far"] for i in rep.issues)


def test_no_corridor_access_when_no_corridors():
    lay = build_demo_layout()
    lay.corridors.clear()
    rep = validate_layout(lay)
    assert {i.entity_ids[0] for i in rep.issues if i.code == "NO_CORRIDOR_ACCESS"} == {s.id for s in lay.workstations}


def test_no_access_when_stub_blocked_by_obstacle():
    lay = build_demo_layout()
    # raw'ın çıkışı (4,3.5): her yukarı yol kolon-benzeri geniş engelle kapatıldı, west koridoru da erişim dışı
    lay.obstacles.append(Obstacle(id="shield", name="Kalkan", rect=Rect(x=0, y=5.2, w=8, h=0.6)))
    assert any(i.code == "NO_CORRIDOR_ACCESS" and i.entity_ids == ["raw"] for i in validate_layout(lay).issues)


def test_corridor_out_of_bounds():
    lay = build_demo_layout()
    lay.corridors.append(Corridor(id="bad", points=[Point(x=0, y=5), Point(x=35, y=5)], width=3))
    assert any(i.code == "OUT_OF_BOUNDS" and i.entity_ids == ["bad"] for i in validate_layout(lay).issues)


def test_report_valid_flag_and_layout_id():
    lay = build_demo_layout()
    rep = validate_layout(lay)
    assert rep.layout_id == lay.id


# ---- editing -----------------------------------------------------------------
def test_move_is_pure():
    lay = build_demo_layout()
    new = move_workstation(lay, "raw", 2, 3, rotation="180")
    assert lay.station("raw").x == 1 and lay.station("raw").rotation == Rotation.R0
    assert new.station("raw").x == 2 and new.station("raw").y == 3 and new.station("raw").rotation == Rotation.R180
    assert new.id == lay.id


def test_move_fixed_and_unknown():
    lay = build_demo_layout()
    lay.workstations[0] = lay.workstations[0].model_copy(update={"fixed": True})
    with pytest.raises(ValueError):
        move_workstation(lay, lay.workstations[0].id, 5, 5)
    with pytest.raises(KeyError):
        move_workstation(lay, "nope", 1, 1)


def test_add_remove():
    lay = build_demo_layout()
    ws = Workstation(id="new", name="Yeni", x=14, y=2, width=3, length=2)
    new = add_workstation(lay, ws)
    assert len(new.workstations) == len(lay.workstations) + 1 and len(lay.workstations) == 7
    with pytest.raises(ValueError):
        add_workstation(new, ws)
    back = remove_workstation(new, "new")
    assert [s.id for s in back.workstations] == [s.id for s in lay.workstations]
    with pytest.raises(KeyError):
        remove_workstation(lay, "ghost")


def test_clone_layout_lineage():
    lay = build_demo_layout()
    c = clone_layout(lay, "v2", "Layout v2")
    assert c.id == "v2" and c.name == "Layout v2" and c.parent_id == lay.id and c.version == lay.version + 1
    c.workstations[0].x = 99   # derin kopya: orijinal etkilenmez
    assert lay.workstations[0].x != 99


def test_terminal_point_rotation():
    # width 4, length 2, giriş offset (-1, 0) -> rotation 0: merkez(2,1) -> (1,1)
    ws = Workstation(id="t", name="t", x=0, y=0, width=4, length=2, input_offset=Point(x=-1, y=0),
                     output_offset=Point(x=1, y=0))
    assert terminal_point(ws, "in") == pytest.approx((1, 1))
    r90 = ws.model_copy(update={"rotation": Rotation.R90})   # ayak izi 2x4 -> merkez (1,2); (-1,0)->(0,-1)
    assert terminal_point(r90, "in") == pytest.approx((1, 1))
    assert terminal_point(r90, "out") == pytest.approx((1, 3))
    r180 = ws.model_copy(update={"rotation": Rotation.R180})
    assert terminal_point(r180, "in") == pytest.approx((3, 1))
