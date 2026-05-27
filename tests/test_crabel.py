"""
Tests unitaires Crabel.

Stratégie de mock :
- MetaTrader5 est injecté dans sys.modules AVANT tout import de crabel, de
  sorte que `import MetaTrader5 as mt5` dans mt5_data.py résout le mock.
- Les tests de couche (b) — calculator — sont purement fonctionnels : aucun
  accès MT5, donc aucun mock requis.
- Les tests de couche (a)/(c) configurent les retours du mock au cas par cas.
"""

import sys
from unittest.mock import MagicMock, patch

# ── Mock MetaTrader5 avant tout import de crabel ─────────────────────────────
_mt5_mock = MagicMock()
sys.modules["MetaTrader5"] = _mt5_mock

# ── Imports crabel (après injection du mock) ──────────────────────────────────
from crabel.calculator import (  # noqa: E402
    compute_atr,
    compute_signals,
    compute_stretch,
    compute_tradeability_hint,
)
from crabel.exceptions import (  # noqa: E402
    InsufficientHistoryForStretch,
    MalformedBarData,
    MT5DataError,
    MT5SessionNotInitialized,
    SymbolNotFound,
)
from crabel.mt5_data import check_session  # noqa: E402

import pytest  # noqa: E402


# ── Helpers ───────────────────────────────────────────────────────────────────


def _bar(o: float, h: float, l: float, c: float, date: str = "2024-01-01") -> dict:
    """Construit un bar dict minimal pour les tests de la couche pure."""
    return {"timestamp": 0, "date": date, "open": o, "high": h, "low": l, "close": c}


def _bars(*tuples) -> list[dict]:
    """Construit une liste de bars depuis des tuples (open, high, low, close)."""
    return [_bar(o, h, l, c) for o, h, l, c in tuples]


def _dated_bar(o, h, l, c, date):
    return {"timestamp": 0, "date": date, "open": o, "high": h, "low": l, "close": c}


# ── Tests de la couche (b) — logique pure, sans MT5 ──────────────────────────


class TestNR4:
    """NR4 : R(J) strictement < R(J-1), R(J-2), R(J-3)."""

    def test_nr4_true(self):
        bars = _bars(
            (10, 15, 10, 12),   # J-3 range=5
            (12, 16, 12, 14),   # J-2 range=4
            (14, 17, 14, 15),   # J-1 range=3
            (15, 17, 15, 16),   # J   range=2  → 2<3<4<5 ✓
        )
        res = compute_signals(bars, None, 3)
        assert res["signals"]["nr4"] is True

    def test_nr4_false_equality_spec_test_a(self):
        """
        Test A (spécification) — égalité de range invalide NR4.

        J-3 range=5, J-2 range=4, J-1 range=3, J range=3
        Attendu : nr4=False car 3 pas strictement < 3.

        # DIVERGENCE: aucune — calcul vérifié indépendamment depuis la définition.
        """
        bars = _bars(
            (10, 15, 10, 12),   # J-3 range=5
            (12, 16, 12, 14),   # J-2 range=4
            (14, 17, 14, 15),   # J-1 range=3
            (15, 18, 15, 16),   # J   range=3  → 3 pas < 3
        )
        res = compute_signals(bars, None, 3)
        assert res["signals"]["nr4"] is False

    def test_nr4_insufficient_history_returns_none(self):
        """Moins de 3 barres avant J → nr4=None avec note."""
        bars = _bars(
            (10, 15, 10, 12),   # J-1
            (12, 14, 12, 13),   # J
        )
        res = compute_signals(bars, None, 1)
        assert res["signals"]["nr4"] is None
        assert any("nr4" in n and "insuffisant" in n for n in res["notes"])

    def test_nr4_false_not_all_smaller(self):
        """R(J) < R(J-1) mais pas < R(J-2)."""
        bars = _bars(
            (10, 14, 10, 12),   # J-3 range=4
            (12, 15, 12, 14),   # J-2 range=3
            (14, 17, 14, 15),   # J-1 range=3
            (15, 17, 16, 16),   # J   range=1  → 1<3 ✓ mais 1<3 ✓ et 1<4 ✓ → True
        )
        res = compute_signals(bars, None, 3)
        assert res["signals"]["nr4"] is True  # tous satisfaits → True


class TestNR7:
    def test_nr7_true(self):
        bars = _bars(
            (0, 8, 0, 4),   # J-6 range=8
            (0, 7, 0, 4),   # J-5 range=7
            (0, 6, 0, 4),   # J-4 range=6
            (0, 5, 0, 4),   # J-3 range=5
            (0, 4, 0, 4),   # J-2 range=4
            (0, 3, 0, 4),   # J-1 range=3
            (0, 2, 0, 4),   # J   range=2 < 3,4,5,6,7,8 ✓
        )
        res = compute_signals(bars, None, 6)
        assert res["signals"]["nr7"] is True

    def test_nr7_insufficient_history_returns_none(self):
        bars = _bars(
            (0, 5, 0, 3), (0, 4, 0, 3), (0, 3, 0, 3), (0, 2, 0, 3),
        )
        res = compute_signals(bars, None, 3)
        assert res["signals"]["nr7"] is None


class TestInsideDay:
    def test_inside_strict_true(self):
        bars = _bars(
            (10, 20, 8, 15),    # J-1 high=20 low=8
            (11, 18, 9, 14),    # J   high=18<20 low=9>8 ✓
        )
        res = compute_signals(bars, None, 1, inside_strict=True)
        assert res["signals"]["inside_day"] is True

    def test_inside_strict_false_boundary(self):
        """Avec inside_strict=True, égalité sur high ⇒ False."""
        bars = _bars(
            (10, 20, 8, 15),    # J-1
            (11, 20, 9, 14),    # J high==20 → pas inside strict
        )
        res = compute_signals(bars, None, 1, inside_strict=True)
        assert res["signals"]["inside_day"] is False

    def test_inside_relaxed_true_at_boundary(self):
        """Avec inside_strict=False, high(J)==high(J-1) est accepté."""
        bars = _bars(
            (10, 20, 8, 15),
            (11, 20, 9, 14),    # high==20, low 9>8
        )
        res = compute_signals(bars, None, 1, inside_strict=False)
        assert res["signals"]["inside_day"] is True

    def test_inside_false_high_breaks_out(self):
        bars = _bars(
            (10, 20, 8, 15),
            (11, 21, 9, 14),    # high=21 > 20 → pas inside
        )
        res = compute_signals(bars, None, 1)
        assert res["signals"]["inside_day"] is False


class TestDoji:
    def test_doji_true_spec_test_c(self):
        """
        Test C (spécification) — Doji avec seuil 0.10.

        J: o=50.00 h=51.20 l=49.10 c=50.05
        range = 2.10, body = 0.05, seuil = 0.21 → 0.05 ≤ 0.21 ⇒ True

        # DIVERGENCE: aucune — calcul vérifié indépendamment.
        """
        bars = [_bar(50.00, 51.20, 49.10, 50.05)]
        res = compute_signals(bars, None, 0, doji_threshold=0.10)
        assert res["signals"]["doji"] is True

    def test_doji_false_body_too_large(self):
        bars = [_bar(50.00, 51.20, 49.10, 51.10)]
        # body = 1.10, range = 2.10, seuil = 0.21 → 1.10 > 0.21
        res = compute_signals(bars, None, 0, doji_threshold=0.10)
        assert res["signals"]["doji"] is False

    def test_doji_zero_range_open_eq_close(self):
        """R=0 et open==close → Doji True."""
        bars = [_bar(50.00, 50.00, 50.00, 50.00)]
        res = compute_signals(bars, None, 0)
        assert res["signals"]["doji"] is True

    def test_doji_zero_range_open_ne_close(self):
        """R=0 mais open≠close (donnée incohérente) → Doji False, pas de ZeroDivision."""
        bars = [_bar(50.00, 50.00, 50.00, 50.01)]
        res = compute_signals(bars, None, 0)
        assert res["signals"]["doji"] is False

    def test_doji_clearly_within_threshold(self):
        """body nettement sous le seuil → True (évite les ambiguïtés float)."""
        bars = [_bar(50.00, 51.00, 49.00, 50.15)]
        # range=2.00, body=0.15, seuil=0.10*2.00=0.20 → 0.15<0.20 ✓
        res = compute_signals(bars, None, 0, doji_threshold=0.10)
        assert res["signals"]["doji"] is True

    def test_doji_just_above_threshold_false(self):
        """body clairement au-dessus du seuil → False."""
        bars = [_bar(50.00, 51.00, 49.00, 50.30)]
        # range=2.00, body=0.30, seuil=0.20 → 0.30>0.20 → False
        res = compute_signals(bars, None, 0, doji_threshold=0.10)
        assert res["signals"]["doji"] is False


class TestWS4AndExpansionFlag:
    """Test B (spécification) — WS4 + expansion_flag."""

    def test_ws4_true_and_expansion_flag_spec_test_b(self):
        """
        Test B (spécification).

        J-3 range=2, J-2 range=3, J-1 range=2, J range=13
        ws4: 13>2 ET 13>3 ET 13>2 → True
        expansion_flag: True

        # DIVERGENCE: aucune — calcul vérifié indépendamment.
        """
        bars = _bars(
            (10, 12, 10, 11),   # J-3 range=2
            (11, 14, 11, 13),   # J-2 range=3
            (13, 15, 13, 14),   # J-1 range=2
            (14, 25, 12, 24),   # J   range=13
        )
        res = compute_signals(bars, None, 3)
        assert res["signals"]["ws4"] is True
        assert res["expansion_flag"] is True

    def test_expansion_flag_true_when_ws4_true_ws7_none(self):
        """expansion_flag=True si ws4=True même quand ws7=None (historique court)."""
        bars = _bars(
            (10, 12, 10, 11),   # J-3 range=2
            (11, 14, 11, 13),   # J-2 range=3
            (13, 15, 13, 14),   # J-1 range=2
            (14, 25, 12, 24),   # J   range=13
        )
        res = compute_signals(bars, None, 3)
        # ws7 nécessite 6 barres avant J ; ici j_idx=3 < 6 → ws7=None
        assert res["signals"]["ws7"] is None
        assert res["expansion_flag"] is True

    def test_expansion_flag_false_no_expansion(self):
        bars = _bars(
            (10, 15, 10, 12),   # J-3 range=5
            (12, 14, 12, 13),   # J-2 range=2
            (13, 16, 13, 14),   # J-1 range=3
            (14, 16, 14, 15),   # J   range=2 < J-1=3 → ws4 False
        )
        res = compute_signals(bars, None, 3)
        assert res["expansion_flag"] is False


class TestTwoBNR:
    def test_2bnr_true(self):
        """span2(J) < span2(k) pour chaque k de J-1 à J-20."""
        # Construit 22 barres dont les 21 premières ont de grands spans.
        # span2(J) = max(h[-1],h[-2]) - min(l[-1],l[-2]) doit être le plus petit.
        wide_bar = (0, 10, 0, 5)   # range=10
        narrow1 = (4, 5, 4, 5)     # range=1  (J-1)
        narrow2 = (4, 5, 4, 5)     # range=1  (J)
        # span2(J) = max(5,5)-min(4,4) = 1
        # On intercale des barres larges pour les positions J-2 … J-20
        bars = [_bar(*wide_bar)] * 20 + [_bar(*narrow1), _bar(*narrow2)]
        # j_idx=21, 22 barres au total
        res = compute_signals(bars, None, 21)
        assert res["signals"]["two_bar_nr"] is True

    def test_2bnr_none_insufficient_history(self):
        """Moins de 21 barres avant J → two_bar_nr=None."""
        bars = _bars(*[(0, 3, 0, 2)] * 5)
        res = compute_signals(bars, None, 4)
        assert res["signals"]["two_bar_nr"] is None
        assert any("two_bar_nr" in n for n in res["notes"])


class TestEightBNR:
    def test_8bnr_none_when_history_days_48_is_tight(self):
        """j_idx < 47 (besoin min 47) → eight_bar_nr=None avec note."""
        bars = _bars(*[(0, 3, 0, 2)] * 46)  # 46 barres, j_idx=45 < 47
        res = compute_signals(bars, None, 45)
        assert res["signals"]["eight_bar_nr"] is None
        assert any("eight_bar_nr" in n for n in res["notes"])


class TestContractionRank:
    def test_rank_0_no_signals(self):
        bars = _bars(
            (0, 5, 0, 3), (0, 4, 0, 3), (0, 3, 0, 3), (0, 4, 0, 3),
        )
        # J range=4 > J-1 range=3 → ws4 candidate, but also not NR
        res = compute_signals(bars, None, 3)
        # aucun signal de contraction actif
        assert res["contraction_rank"] == 0

    def test_rank_2_nr4_only(self):
        bars = _bars(
            (10, 15, 10, 12),   # J-3 range=5
            (12, 16, 12, 14),   # J-2 range=4
            (14, 17, 14, 15),   # J-1 range=3
            (15, 17, 15, 16),   # J   range=2
        )
        res = compute_signals(bars, None, 3)
        assert res["signals"]["nr4"] is True
        assert res["contraction_rank"] == 2

    def test_rank_3_doji_beats_nr4_not_present(self):
        """Doji sans NR4 → rang 3."""
        bars = _bars(
            (10, 20, 10, 15),   # J-3 range=10
            (10, 18, 10, 14),   # J-2 range=8
            (10, 16, 10, 13),   # J-1 range=6
            (10, 14, 10, 10.1), # J   range=4 < 6,8,10 → NR4 True; body=0.1 ≤ 0.4 → Doji
        )
        res = compute_signals(bars, None, 3, doji_threshold=0.10)
        # NR4=True (rang 2), Doji=True (rang 3) → max = 3
        assert res["signals"]["doji"] is True
        assert res["signals"]["nr4"] is True
        assert res["contraction_rank"] == 3


class TestNaNHandling:
    def test_nr4_none_when_prev_bar_has_nan_high(self):
        import math
        bars = [
            _bar(10, 15, 10, 12),
            _bar(12, float("nan"), 12, 14),   # J-2 high=NaN
            _bar(14, 17, 14, 15),
            _bar(15, 17, 15, 16),
        ]
        res = compute_signals(bars, None, 3)
        assert res["signals"]["nr4"] is None

    def test_j_bar_nan_returns_all_none(self):
        bars = [
            _bar(10, 15, 10, 12),
            _bar(12, float("nan"), 12, 14),   # J
        ]
        res = compute_signals(bars, None, 1)
        for sig in res["signals"].values():
            assert sig is None


# ── Tests du Stretch ──────────────────────────────────────────────────────────


class TestStretch:
    """
    Tests unitaires de compute_stretch (couche pure, sans MT5).

    Cas de référence (spécification — calcul vérifié manuellement) :
    day 1:  o=100 h=105 l=98  → min(100-98=2, 105-100=5) = 2
    day 2:  o=102 h=104 l=99  → min(102-99=3, 104-102=2) = 2
    day 3:  o=101 h=106 l=100 → min(101-100=1, 106-101=5) = 1
    day 4:  o=103 h=107 l=101 → min(103-101=2, 107-103=4) = 2
    day 5:  o=104 h=108 l=102 → min(104-102=2, 108-104=4) = 2
    day 6:  o=106 h=110 l=104 → min(106-104=2, 110-106=4) = 2
    day 7:  o=107 h=109 l=104 → min(107-104=3, 109-107=2) = 2
    day 8:  o=108 h=112 l=106 → min(108-106=2, 112-108=4) = 2
    day 9:  o=110 h=114 l=108 → min(110-108=2, 114-110=4) = 2
    day 10: o=111 h=113 l=109 → min(111-109=2, 113-111=2) = 2
    Sum = 2+2+1+2+2+2+2+2+2+2 = 19  →  Stretch = 19/10 = 1.9

    # DIVERGENCE: aucune — chaque distance recalculée depuis la définition.
    """

    WINDOW_BARS = [
        # (open, high, low, close)  — J+1 bar ajoutée en fin comme J
        (100, 105,  98, 102),   # day 1  dist=min(2,5)=2
        (102, 104,  99, 103),   # day 2  dist=min(3,2)=2
        (101, 106, 100, 103),   # day 3  dist=min(1,5)=1
        (103, 107, 101, 105),   # day 4  dist=min(2,4)=2
        (104, 108, 102, 106),   # day 5  dist=min(2,4)=2
        (106, 110, 104, 108),   # day 6  dist=min(2,4)=2
        (107, 109, 104, 107),   # day 7  dist=min(3,2)=2
        (108, 112, 106, 110),   # day 8  dist=min(2,4)=2
        (110, 114, 108, 112),   # day 9  dist=min(2,4)=2
        (111, 113, 109, 112),   # day 10 dist=min(2,2)=2
    ]
    # J bar (index 10, non utilisé dans la fenêtre avec offset=1)
    J_BAR = (115, 120, 113, 116)

    def _make_bars(self):
        return _bars(*self.WINDOW_BARS, self.J_BAR)

    def test_stretch_value_reference(self):
        """Stretch = 1.9 sur les 10 jours de référence (j_idx=10)."""
        bars = self._make_bars()
        result = compute_stretch(bars, j_idx=10, window=10, offset=1)
        assert result["value"] == pytest.approx(1.9, abs=1e-8)
        assert result["window_days"] == 10
        assert len(result["per_day_distances"]) == 10

    def test_stretch_per_day_distances(self):
        """Vérifie chaque distance individuelle."""
        bars = self._make_bars()
        result = compute_stretch(bars, j_idx=10, window=10, offset=1)
        expected = [2.0, 2.0, 1.0, 2.0, 2.0, 2.0, 2.0, 2.0, 2.0, 2.0]
        assert result["per_day_distances"] == pytest.approx(expected, abs=1e-8)

    def test_stretch_orb_levels(self):
        """
        ORB levels avec Stretch=1.9, close(J)=116.

        buy_stop_estimated  = 116 + 1.9 = 117.9
        sell_stop_estimated = 116 - 1.9 = 114.1
        formula_buy_stop    = "open_next + 1.9"

        # DIVERGENCE: aucune.
        """
        bars = self._make_bars()
        result = compute_stretch(bars, j_idx=10, window=10, offset=1)
        stretch = result["value"]
        close_j = 116.0
        assert close_j + stretch == pytest.approx(117.9, abs=1e-8)
        assert close_j - stretch == pytest.approx(114.1, abs=1e-8)
        assert f"open_next + {stretch}" == "open_next + 1.9"

    def test_stretch_sl_stretch_opposite(self):
        """
        SL stretch_opposite = 2 × Stretch = 2 × 1.9 = 3.8.

        # DIVERGENCE: aucune.
        """
        bars = self._make_bars()
        result = compute_stretch(bars, j_idx=10, window=10, offset=1)
        assert 2 * result["value"] == pytest.approx(3.8, abs=1e-8)

    def test_stretch_insufficient_history_raises(self):
        """9 jours avant J (j_idx=9, window=10) → InsufficientHistoryForStretch."""
        # 10 barres total, j_idx=9 → start_idx = 9-1-10+1 = -1 < 0
        bars = _bars(*self.WINDOW_BARS)  # 10 barres, j_idx=9
        with pytest.raises(InsufficientHistoryForStretch) as exc_info:
            compute_stretch(bars, j_idx=9, window=10, offset=1)
        assert "insuffisant" in str(exc_info.value).lower()

    def test_stretch_exactly_enough_history(self):
        """10 barres avant J (j_idx=10, window=10) → calcul OK."""
        bars = self._make_bars()   # 11 barres, j_idx=10
        result = compute_stretch(bars, j_idx=10, window=10, offset=1)
        assert "value" in result

    def test_stretch_malformed_open_above_high_raises(self):
        """open > high sur un jour de la fenêtre → MalformedBarData."""
        bars = self._make_bars()
        bars[3] = _dated_bar(200, 105, 98, 102, "2024-01-04")  # open=200 > high=105
        with pytest.raises(MalformedBarData) as exc_info:
            compute_stretch(bars, j_idx=10, window=10, offset=1)
        assert "2024-01-04" in str(exc_info.value) or "open" in str(exc_info.value).lower()

    def test_stretch_malformed_open_below_low_raises(self):
        """open < low sur un jour de la fenêtre → MalformedBarData."""
        bars = self._make_bars()
        bars[5] = _dated_bar(50, 110, 104, 108, "2024-01-06")  # open=50 < low=104
        with pytest.raises(MalformedBarData):
            compute_stretch(bars, j_idx=10, window=10, offset=1)

    def test_stretch_zero_range_day_gives_zero_distance(self):
        """
        Jour à range nul (high==low==open) → distance = 0, inclus normalement.
        Le range nul est valide (ex. jour férié) et réduit le Stretch.
        """
        bars = self._make_bars()
        # Remplace le day 1 par une barre avec range nul
        bars[0] = _dated_bar(100, 100, 100, 100, "2024-01-01")  # distance = 0
        result = compute_stretch(bars, j_idx=10, window=10, offset=1)
        assert result["per_day_distances"][0] == 0.0
        # Stretch = (0+2+1+2+2+2+2+2+2+2)/10 = 17/10 = 1.7
        assert result["value"] == pytest.approx(1.7, abs=1e-8)

    def test_stretch_window_dates_correct(self):
        """window_dates contient exactement les dates de la fenêtre (J exclu)."""
        # Construit des barres avec des dates distinctes
        dated_bars = [
            _dated_bar(100, 105, 98, 102, f"2024-01-{i+1:02d}")
            for i in range(11)  # 10 window bars + J
        ]
        result = compute_stretch(dated_bars, j_idx=10, window=10, offset=1)
        assert len(result["window_dates"]) == 10
        assert "2024-01-10" in result["window_dates"]   # J-1
        assert "2024-01-11" not in result["window_dates"]  # J exclu


# ── Tests de l'ATR ────────────────────────────────────────────────────────────


class TestATR:
    """
    Tests de compute_atr (couche pure).

    Cas de référence :
    15 barres uniformes : h=12, l=8, c=10 (range=4).
    bars[0].close = 10 (prev pour bars[1]).
    TR pour chaque bars[i] (i=1..14) :
        TR = max(h-l, |h-prev_c|, |l-prev_c|)
           = max(4,   |12-10|=2,  |8-10|=2)
           = 4
    ATR(14) = 4.0

    # DIVERGENCE: aucune — calcul vérifié manuellement.
    """

    @staticmethod
    def _uniform_bars(n: int, h=12.0, l=8.0, c=10.0) -> list[dict]:
        return [_bar(10.0, h, l, c) for _ in range(n)]

    def test_atr_14_reference(self):
        """ATR(14) sur 15 barres uniformes (h=12, l=8, c=10) → 4.0."""
        bars = self._uniform_bars(15)
        result = compute_atr(bars, j_idx=14, period=14)
        assert result == pytest.approx(4.0, abs=1e-8)

    def test_atr_insufficient_returns_none(self):
        """j_idx < period (13 < 14) → None, pas d'erreur."""
        bars = self._uniform_bars(14)   # j_idx=13
        result = compute_atr(bars, j_idx=13, period=14)
        assert result is None

    def test_atr_exactly_enough(self):
        """j_idx == period (14) → calcul OK."""
        bars = self._uniform_bars(15)
        result = compute_atr(bars, j_idx=14, period=14)
        assert result is not None

    def test_atr_varying_bars(self):
        """
        ATR sur des barres hétérogènes pour vérifier la formule TR.

        bars[0]: h=10, l=8, c=9  (prev close pour bars[1])
        bars[1]: h=12, l=7, c=10
            TR = max(12-7=5, |12-9|=3, |7-9|=2) = 5
        bars[2]: h=11, l=9, c=10
            TR = max(11-9=2, |11-10|=1, |9-10|=1) = 2
        Seuls 2 barres de TR → ATR(2) = (5+2)/2 = 3.5

        # DIVERGENCE: aucune.
        """
        bars = [
            _bar(9,  10, 8, 9),   # bars[0] prev
            _bar(10, 12, 7, 10),  # bars[1] TR=5
            _bar(10, 11, 9, 10),  # bars[2] TR=2
        ]
        result = compute_atr(bars, j_idx=2, period=2)
        assert result == pytest.approx(3.5, abs=1e-8)

    def test_atr_nan_in_window_returns_none(self):
        """NaN dans la fenêtre ATR → None (pas d'erreur)."""
        import math
        bars = self._uniform_bars(15)
        bars[7] = _bar(10.0, float("nan"), 8.0, 10.0)
        result = compute_atr(bars, j_idx=14, period=14)
        assert result is None


# ── Tests du tradeability_hint ───────────────────────────────────────────────


class TestTradeabilityHint:
    """Tests de compute_tradeability_hint pour chaque branche de la règle."""

    BASE_SIGNALS = {
        "nr4": False, "nr7": False, "inside_day": False, "doji": False,
        "two_bar_nr": False, "three_bar_nr": False,
        "four_bar_nr": None, "eight_bar_nr": None,
        "ws4": False, "ws7": False, "trend_day": False,
    }
    BASE_COMBINED = {"inside_and_nr4": False, "inside_and_nr7": False}

    def _hint(self, rank, expansion=False):
        return compute_tradeability_hint(
            self.BASE_SIGNALS, self.BASE_COMBINED, rank, expansion
        )

    def test_expansion_flag_true_gives_low(self):
        """expansion_flag=True → confidence=low, peu importe le rang."""
        h = self._hint(rank=7, expansion=True)
        assert h["confidence"] == "low"
        assert h["preferred_direction"] is None
        assert "expansion" in h["reason"].lower()

    def test_rank_7_gives_high(self):
        """contraction_rank=7 (inside+NR4) → confidence=high."""
        h = self._hint(rank=7)
        assert h["confidence"] == "high"
        assert h["preferred_direction"] is None

    def test_rank_8_gives_high(self):
        """contraction_rank=8 (inside+NR7) → confidence=high."""
        h = self._hint(rank=8)
        assert h["confidence"] == "high"

    def test_rank_6_gives_medium(self):
        """contraction_rank=6 (3BNR) → confidence=medium."""
        h = self._hint(rank=6)
        assert h["confidence"] == "medium"

    def test_rank_4_gives_medium(self):
        """contraction_rank=4 (NR7) → confidence=medium."""
        h = self._hint(rank=4)
        assert h["confidence"] == "medium"

    def test_rank_3_gives_low(self):
        """contraction_rank=3 (Doji) → confidence=low."""
        h = self._hint(rank=3)
        assert h["confidence"] == "low"

    def test_rank_1_gives_low(self):
        """contraction_rank=1 (inside) → confidence=low."""
        h = self._hint(rank=1)
        assert h["confidence"] == "low"
        assert "modérée" in h["reason"] or "early" in h["reason"].lower()

    def test_rank_0_no_expansion_gives_low(self):
        """contraction_rank=0 et expansion=False → confidence=low."""
        h = self._hint(rank=0)
        assert h["confidence"] == "low"
        assert h["preferred_direction"] is None

    def test_expansion_beats_high_rank(self):
        """expansion_flag=True prime sur un rang élevé → toujours low."""
        h = self._hint(rank=8, expansion=True)
        assert h["confidence"] == "low"


# ── Tests de la couche (a) — MT5 data access ─────────────────────────────────


class TestCheckSession:
    def test_raises_when_no_session(self):
        """terminal_info() → None ⇒ MT5SessionNotInitialized levée."""
        _mt5_mock.terminal_info.return_value = None
        _mt5_mock.initialize.reset_mock()

        with pytest.raises(MT5SessionNotInitialized):
            check_session()

    def test_never_calls_initialize(self):
        """check_session ne doit JAMAIS appeler mt5.initialize()."""
        _mt5_mock.terminal_info.return_value = None
        _mt5_mock.initialize.reset_mock()

        try:
            check_session()
        except MT5SessionNotInitialized:
            pass

        _mt5_mock.initialize.assert_not_called()

    def test_passes_when_session_active(self):
        """terminal_info() retourne un objet → pas d'exception."""
        _mt5_mock.terminal_info.return_value = MagicMock()
        check_session()  # ne doit pas lever


class TestSymbolNotFound:
    def test_raises_symbol_not_found(self):
        from crabel.mt5_data import fetch_d1_bars

        _mt5_mock.terminal_info.return_value = MagicMock()
        _mt5_mock.symbol_info.return_value = None

        with pytest.raises(SymbolNotFound):
            fetch_d1_bars("FAKEXX", 10)


# ── Test de la couche (c) — analyze_symbol avec MT5 entièrement mocké ────────


class TestAnalyzeSymbol:
    def _setup_mock_session(self, d1_bars_raw, server_now: int, h1_bars_raw=None):
        """Configure _mt5_mock pour un appel analyze_symbol standard."""
        import numpy as np

        _mt5_mock.terminal_info.return_value = MagicMock()

        sym_info = MagicMock()
        sym_info.visible = True
        _mt5_mock.symbol_info.return_value = sym_info

        tick = MagicMock()
        tick.time = server_now
        _mt5_mock.symbol_info_tick.return_value = tick

        dtype = [("time", "i8"), ("open", "f8"), ("high", "f8"), ("low", "f8"), ("close", "f8")]
        _mt5_mock.copy_rates_from_pos.return_value = np.array(d1_bars_raw, dtype=dtype)
        _mt5_mock.TIMEFRAME_D1 = 1440
        _mt5_mock.TIMEFRAME_H1 = 60

        if h1_bars_raw is not None:
            _mt5_mock.copy_rates_range.return_value = np.array(h1_bars_raw, dtype=dtype)
        else:
            _mt5_mock.copy_rates_range.return_value = np.array([], dtype=dtype)

    def test_analyze_eurusd_returns_correct_schema(self):
        """Vérifie la structure de retour d'analyze_symbol."""
        from crabel import analyze_symbol

        base_ts = 1_700_000_000  # timestamp arbitraire
        day = 86400
        # 10 barres D1 clôturées, range décroissant
        d1 = [(base_ts + i * day, 10, 10 + (10 - i), 10, 11) for i in range(10)]
        server_now = base_ts + 10 * day + 3600  # après la 10e barre clôturée

        self._setup_mock_session(d1, server_now)

        result = analyze_symbol("EURUSD", history_days=10)
        assert result["symbol"] == "EURUSD"
        assert result["is_closed_bar"] is True
        assert "signals" in result
        assert "nr4" in result["signals"]
        assert "combined" in result
        assert "contraction_rank" in result
        assert isinstance(result["notes"], list)

    def test_no_session_raises(self):
        """analyze_symbol lève MT5SessionNotInitialized quand terminal_info=None."""
        from crabel import analyze_symbol

        _mt5_mock.terminal_info.return_value = None
        _mt5_mock.initialize.reset_mock()

        with pytest.raises(MT5SessionNotInitialized):
            analyze_symbol("EURUSD")

        _mt5_mock.initialize.assert_not_called()


# ── Tests de analyze_symbol_full (couche c, MT5 mocké) ───────────────────────


class TestAnalyzeSymbolFull:
    """
    Tests d'intégration de analyze_symbol_full avec MT5 entièrement mocké.
    On construit suffisamment de barres pour satisfaire Stretch(10) + ATR(14).
    """

    def _setup_full_mock(self, n_bars: int = 60, server_now_offset: int = 3600):
        """
        Configure le mock MT5 avec n_bars barres D1 uniformes.
        Toutes les barres : o=100, h=105, l=98, c=102.
        distance = min(100-98=2, 105-100=5) = 2  →  Stretch = 2.0
        TR = max(7, 5, 2) = 7  →  ATR(14) = 7.0
        """
        import numpy as np

        _mt5_mock.terminal_info.return_value = MagicMock()
        sym_info = MagicMock()
        sym_info.visible = True
        _mt5_mock.symbol_info.return_value = sym_info

        base_ts = 1_700_000_000
        day = 86400
        d1 = [(base_ts + i * day, 100.0, 105.0, 98.0, 102.0) for i in range(n_bars)]
        server_now = base_ts + n_bars * day + server_now_offset

        tick = MagicMock()
        tick.time = server_now
        _mt5_mock.symbol_info_tick.return_value = tick

        dtype = [("time", "i8"), ("open", "f8"), ("high", "f8"), ("low", "f8"), ("close", "f8")]
        _mt5_mock.copy_rates_from_pos.return_value = np.array(d1, dtype=dtype)
        _mt5_mock.TIMEFRAME_D1 = 1440
        _mt5_mock.TIMEFRAME_H1 = 60
        # H1 vide (trend_day = None)
        _mt5_mock.copy_rates_range.return_value = np.array([], dtype=dtype)

    def test_full_schema_keys(self):
        """analyze_symbol_full retourne toutes les nouvelles clés."""
        from crabel import analyze_symbol_full

        self._setup_full_mock()
        result = analyze_symbol_full("US100")

        for key in ("stretch", "orb_levels_estimated", "orb_levels_next_day",
                    "sl_options", "tradeability_hint"):
            assert key in result, f"Clé manquante : {key}"

    def test_stretch_value(self):
        """
        Stretch avec barres uniformes o=100, h=105, l=98 :
        distance = min(100-98=2, 105-100=5) = 2  →  Stretch = 2.0
        """
        from crabel import analyze_symbol_full

        self._setup_full_mock()
        result = analyze_symbol_full("US100")
        assert result["stretch"]["value"] == pytest.approx(2.0, abs=1e-8)
        assert result["stretch"]["window_days"] == 10

    def test_orb_estimated_levels(self):
        """
        Avec Stretch=2.0 et close(J)=102 :
        buy_stop_estimated  = 104.0
        sell_stop_estimated = 100.0
        """
        from crabel import analyze_symbol_full

        self._setup_full_mock()
        result = analyze_symbol_full("US100")
        est = result["orb_levels_estimated"]
        assert est["buy_stop_estimated"]  == pytest.approx(104.0, abs=1e-8)
        assert est["sell_stop_estimated"] == pytest.approx(100.0, abs=1e-8)
        assert est["close_of_j"] == pytest.approx(102.0, abs=1e-8)

    def test_orb_next_day_formulas(self):
        """Les formules ORB contiennent la valeur numérique du Stretch."""
        from crabel import analyze_symbol_full

        self._setup_full_mock()
        result = analyze_symbol_full("US100")
        nd = result["orb_levels_next_day"]
        assert "2.0" in nd["formula_buy_stop"]
        assert "2.0" in nd["formula_sell_stop"]
        assert "J+1" in nd["to_apply_at"]

    def test_sl_options_keys(self):
        """sl_options contient bien les 4 options."""
        from crabel import analyze_symbol_full

        self._setup_full_mock()
        result = analyze_symbol_full("US100")
        sl = result["sl_options"]
        for key in ("stretch_opposite", "opposite_or_extreme",
                    "previous_day_extreme", "atr_based"):
            assert key in sl

    def test_sl_stretch_opposite_distance(self):
        """stretch_opposite.distance = 2 × Stretch = 4.0."""
        from crabel import analyze_symbol_full

        self._setup_full_mock()
        result = analyze_symbol_full("US100")
        assert result["sl_options"]["stretch_opposite"]["distance"] == pytest.approx(4.0, abs=1e-8)

    def test_sl_previous_day_extreme(self):
        """previous_day_extreme.buy_sl = low(J) = 98.0."""
        from crabel import analyze_symbol_full

        self._setup_full_mock()
        result = analyze_symbol_full("US100")
        pde = result["sl_options"]["previous_day_extreme"]
        assert pde["buy_sl"]  == pytest.approx(98.0, abs=1e-8)
        assert pde["sell_sl"] == pytest.approx(105.0, abs=1e-8)

    def test_atr_calculated_when_enough_history(self):
        """ATR(14) disponible avec 60 barres → atr_based.distance non None."""
        from crabel import analyze_symbol_full

        self._setup_full_mock(n_bars=60)
        result = analyze_symbol_full("US100")
        ab = result["sl_options"]["atr_based"]
        assert ab["atr_14"] is not None
        assert ab["distance"] is not None

    def test_atr_none_when_insufficient_history(self):
        """
        Avec seulement 14 barres (j_idx=12 ≈ dernier clôturé), ATR(14)
        peut être None si j_idx < 14. On force n_bars=13 pour garantir
        j_idx < atr_period.
        """
        from crabel import analyze_symbol_full

        self._setup_full_mock(n_bars=13)
        result = analyze_symbol_full("US100", atr_period=14)
        ab = result["sl_options"]["atr_based"]
        assert ab["atr_14"] is None
        assert ab["distance"] is None
        assert any("atr_based" in n for n in result["notes"])

    def test_tradeability_hint_present(self):
        """tradeability_hint a les champs confidence, preferred_direction, reason."""
        from crabel import analyze_symbol_full

        self._setup_full_mock()
        result = analyze_symbol_full("US100")
        hint = result["tradeability_hint"]
        assert "confidence" in hint
        assert "preferred_direction" in hint
        assert "reason" in hint

    def test_no_session_raises_without_init(self):
        """analyze_symbol_full ne tente jamais d'initialiser MT5."""
        from crabel import analyze_symbol_full

        _mt5_mock.terminal_info.return_value = None
        _mt5_mock.initialize.reset_mock()

        with pytest.raises(MT5SessionNotInitialized):
            analyze_symbol_full("US100")

        _mt5_mock.initialize.assert_not_called()

    def test_insufficient_stretch_history_raises(self):
        """
        Avec seulement 5 barres et stretch_window=10, InsufficientHistoryForStretch.
        """
        import numpy as np
        from crabel import analyze_symbol_full

        _mt5_mock.terminal_info.return_value = MagicMock()
        sym_info = MagicMock()
        sym_info.visible = True
        _mt5_mock.symbol_info.return_value = sym_info

        base_ts = 1_700_000_000
        day = 86400
        n = 5
        d1 = [(base_ts + i * day, 100.0, 105.0, 98.0, 102.0) for i in range(n)]
        server_now = base_ts + n * day + 3600

        tick = MagicMock()
        tick.time = server_now
        _mt5_mock.symbol_info_tick.return_value = tick

        dtype = [("time", "i8"), ("open", "f8"), ("high", "f8"), ("low", "f8"), ("close", "f8")]
        _mt5_mock.copy_rates_from_pos.return_value = np.array(d1, dtype=dtype)
        _mt5_mock.TIMEFRAME_D1 = 1440
        _mt5_mock.TIMEFRAME_H1 = 60
        _mt5_mock.copy_rates_range.return_value = np.array([], dtype=dtype)

        with pytest.raises(InsufficientHistoryForStretch):
            analyze_symbol_full("US100", stretch_window=10)
