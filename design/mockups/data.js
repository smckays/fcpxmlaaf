// Sample project track layout (from the converter's preview of Test Cut v1), shared by the mockups.
var DUR = 50;
var TRACKS = [
  ["DX 1", [["Interview A001", 0, 5.5], ["Recorder 4ch", 4.5, 13, "gain", "xf"], ["J-cut Interview", 26, 31]]],
  ["DX 2", [["Interview A001", 0, 5.5], ["Recorder 4ch", 4.5, 13, "gain", "xf"], ["J-cut Interview", 26, 31]]],
  ["DX 3", [["Recorder 4ch", 4.5, 13, "gain"]]],
  ["DX 4", [["Recorder 4ch", 4.5, 13, "gain"]]],
  ["MX 1", [["Music Cue", 1, 21, "gain"]]],
  ["MX 2", [["Music Cue", 1, 21, "gain"]]],
  ["FX 1", [["SFX Door", 13, 21, "gain"], ["SFX Door 2", 23.5, 26]]],
  ["FX 2", [["SFX Wind", 14, 18, "gain"]]]
];
function tc(s) { var f = Math.round((s % 1) * 24), x = Math.floor(s); return "01:00:" + (x < 10 ? "0" : "") + x + ":" + (f < 10 ? "0" : "") + f; }
function render(o) {
  var r = document.getElementById(o.ruler), h = "";
  for (var s = 0; s <= DUR; s += 5) {
    h += '<i style="left:' + (s / DUR * 100) + '%"></i>';
    if (s % 10 === 0 && s < DUR) h += '<span style="left:calc(' + (s / DUR * 100) + '% + 4px)">' + (o.short ? "0:" + (s < 10 ? "0" : "") + s : tc(s)) + "</span>";
  }
  r.innerHTML = h;
  var t = document.getElementById(o.tracks), out = "";
  TRACKS.forEach(function (tr) {
    var light = (o.light || []).some(function (p) { return tr[0].indexOf(p) === 0 });
    out += '<div class="trk"><b>' + tr[0] + '</b><div class="lane">';
    tr[1].forEach(function (c) {
      out += '<div class="clip' + (light ? " lt" : "") + '" style="left:' + (c[1] / DUR * 100) + "%;width:" + ((c[2] - c[1]) / DUR * 100) + '%' + (c[4] === "xf" ? ";padding-left:calc(" + (1 / DUR * 100) + '% + 8px)' : "") + '">' +
        (c[3] === "gain" && !o.noGain ? '<span class="gain"></span>' : "") + c[0] + "</div>";
      if (c[4] === "xf") out += '<div class="xf" style="left:' + (c[1] / DUR * 100) + "%;width:" + (1 / DUR * 100) + '%"></div>';
    });
    out += "</div></div>";
  });
  t.innerHTML = out;
}
