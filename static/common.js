// Shared between index.html (today's dashboard) and trends.html (history/comparison) -
// anything only one page needs stays local to that page's own script instead.

function fmtMoney(v) {
  return "$" + Number(v).toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 });
}
function fmtInt(v) { return Number(v).toLocaleString(); }
function fmtSigned(v) {
  const n = Number(v) || 0;
  return (n > 0 ? "+" : "") + n.toLocaleString(undefined, { maximumFractionDigits: 2 });
}
function fmtPercent(v) { return Number(v).toFixed(1) + "%"; }
function fmtSeconds(v) {
  const s = Math.round(Number(v) || 0);
  if (s < 60) return s + "s";
  return Math.floor(s / 60) + "m " + (s % 60) + "s";
}

function fieldMap(entries) {
  const m = {};
  for (const e of entries) m[e.fieldCode] = e.value;
  return m;
}

function hourLabelFromInt(h) {
  const hh = ((h % 24) + 24) % 24;
  if (hh === 0) return "12 AM";
  if (hh === 12) return "12 PM";
  return hh < 12 ? hh + " AM" : (hh - 12) + " PM";
}

// Hour-of-day computed from the store's configured UTC offset, not the viewer's
// browser locale - keeps shift bucketing correct regardless of where this is opened from.
function localHour24(isoStart, utcOffsetHours) {
  const d = new Date(isoStart);
  return ((d.getUTCHours() + utcOffsetHours) % 24 + 24) % 24;
}

const SHIFT_DEFS = [
  { key: "morning", name: "Morning" },
  { key: "mid", name: "Mid" },
  { key: "night", name: "Night" },
];

function classifyShift(hour24, cfg) {
  // Pre-open hours (overnight cleanup by the closing crew) count as night.
  if (hour24 >= cfg.nightStartHour || hour24 < cfg.openHour) return "night";
  if (hour24 >= cfg.midStartHour) return "mid";
  return "morning";
}

function shiftTimeRange(key, cfg) {
  if (key === "morning") return `${hourLabelFromInt(cfg.openHour)} - ${hourLabelFromInt(cfg.midStartHour)}`;
  if (key === "mid") return `${hourLabelFromInt(cfg.midStartHour)} - ${hourLabelFromInt(cfg.nightStartHour)}`;
  return `${hourLabelFromInt(cfg.nightStartHour)} - ${hourLabelFromInt(cfg.closeHour)}`;
}

function computeShiftSummary(darData, cfg) {
  const shifts = {};
  for (const { key } of SHIFT_DEFS) shifts[key] = { sales: 0, hours: 0, laborCost: 0, bucketCount: 0 };

  for (const hour of darData.dar) {
    const f = fieldMap(hour.data);
    const key = classifyShift(localHour24(hour.startTime, cfg.utcOffsetHours), cfg);
    const sales = Number(f.net_sales || 0);
    const hrs = Number(f.actual_punch_hours || 0);
    const pct = Number(f.labour_percentage_of_sales || 0);
    const s = shifts[key];
    s.sales += sales;
    s.hours += hrs;
    // LifeLenz gives us labor % of sales per hour but not raw labor $ cost, so
    // reconstruct an approximate cost (pct is defined as cost/sales) to get a
    // correctly sales-weighted shift-level percentage instead of a naive average.
    s.laborCost += (pct / 100) * sales;
    s.bucketCount += 1;
  }

  for (const key of Object.keys(shifts)) {
    const s = shifts[key];
    s.laborPct = s.sales > 0 ? (s.laborCost / s.sales) * 100 : 0;
    s.avgStaff = s.bucketCount > 0 ? s.hours / s.bucketCount : 0;
  }
  return shifts;
}

// timeZone: "UTC" matters here, not just style - a day-bucket's ISO timestamp already has
// the correct calendar date baked in server-side (business_day_window did that math), so
// reading it through the *browser's* local timezone instead of UTC could silently shift
// the displayed date by a day depending on where this is opened from.
function formatDayLabel(iso) {
  const d = new Date(iso);
  return d.toLocaleDateString(undefined, { month: "short", day: "numeric", timeZone: "UTC" });
}

function ordinalSuffix(n) {
  if (n % 10 === 1 && n % 100 !== 11) return "st";
  if (n % 10 === 2 && n % 100 !== 12) return "nd";
  if (n % 10 === 3 && n % 100 !== 13) return "rd";
  return "th";
}

function formatFullDate(iso) {
  const d = new Date(iso);
  const weekday = d.toLocaleDateString(undefined, { weekday: "long", timeZone: "UTC" });
  const month = d.toLocaleDateString(undefined, { month: "long", timeZone: "UTC" });
  const day = d.getUTCDate();
  return `${weekday}, ${month} ${day}${ordinalSuffix(day)}`;
}

let shiftConfig = null;
const shiftConfigPromise = fetch("/api/shift-config").then(r => r.json()).then(cfg => { shiftConfig = cfg; return cfg; });

function escapeHtml(s) {
  const div = document.createElement("div");
  div.textContent = s ?? "";
  return div.innerHTML;
}

// Same idea as localHour24 but keeps minutes, since floor-manager matching below
// needs to tell 3:30pm from 4:00pm, not just "afternoon".
function localHourDecimal(isoStart, utcOffsetHours) {
  const d = new Date(isoStart);
  const raw = d.getUTCHours() + d.getUTCMinutes() / 60 + utcOffsetHours;
  return ((raw % 24) + 24) % 24;
}

function formatClockTime(isoStart, utcOffsetHours) {
  const h = localHourDecimal(isoStart, utcOffsetHours);
  const hh24 = Math.floor(h);
  const mm = Math.round((h - hh24) * 60) % 60;
  const period = hh24 < 12 ? "AM" : "PM";
  const hh12 = hh24 % 12 === 0 ? 12 : hh24 % 12;
  return `${hh12}:${String(mm).padStart(2, "0")} ${period}`;
}

// Staff shift types don't share the sales-daypart boundaries used by classifyShift
// (5am/11am/4pm, plus a "pre-open hours belong to last night's closing crew" wraparound
// that only makes sense for backward-looking labor accounting). A crew member's actual
// named shift is grouped by where its start time falls relative to the known manager
// shift starts: night managers begin ~3:30pm, mid managers ~7-8am, so those become the
// bucket edges - not because they're "correct" in the abstract, just because they're the
// only fixed reference points known here. No pre-open wraparound: a shift starting at
// 4:30am is a morning shift, full stop, not leftover from last night.
function classifyStaffShift(shift, cfg) {
  const start = localHourDecimal(shift.shiftStartTime, cfg.utcOffsetHours);
  if (start >= 15) return "night";
  if (start >= 6.5) return "mid";
  return "morning";
}

// isManager is a LifeLenz field; GM and manager-in-training aren't tracked by LifeLenz at
// all (the GENERAL MANAGER role exists in the catalog but isn't actually assigned to
// anyone in the data), so both are manually maintained name lists in .env instead.
function nameIn(list, name) {
  return list.some((n) => n.toLowerCase() === name.toLowerCase());
}

function isRealManager(shift, cfg) {
  return (
    shift.isManager ||
    nameIn(cfg.manualManagers, shift.name) ||
    nameIn(cfg.generalManagers, shift.name)
  );
}

function staffTag(shift, cfg) {
  if (nameIn(cfg.generalManagers, shift.name)) return " (GM)";
  if (shift.isManager || nameIn(cfg.manualManagers, shift.name)) return " (mgr)";
  if (nameIn(cfg.managersInTraining, shift.name)) return " (MIT)";
  return "";
}

// "Floor manager" isn't a field LifeLenz exposes (isShiftRunner is unused at this store,
// role tags are a shared skills list rather than a per-shift position, and Employment.isManager
// misses managers-in-training who run shifts without being flagged as a manager - confirmed by
// Riley Peel's Aug 10 shift: 3:30pm-11:30pm with PRE-SHIFT+FLOOR tags, isManager=false, and
// genuinely the floor manager that night). So this matches on shift-time pattern, but only
// among people already tagged manager/GM/MIT some way - Kilie Bartholomew is strictly crew
// with no tag at all and got a false "guess" on Aug 10 morning before this gate existed,
// which is worse than not guessing. Night's window has to stay narrow (+/-15min around 3:30pm)
// specifically because 4:00pm is a very common ordinary closing-shift start time - a wider
// band caught regular crew (Kyle Eacker, Parker Hubert, Ja'Niya Kempker all start exactly
// 4:00pm) as false positives on Aug 10 before this was tightened. Mid covers both observed
// start times (7am/8am-5pm), looser since it's "almost always" rather than "literally always".
// Mid must end at 5 or 6pm specifically - 4pm was another false-positive source (regular
// mid-shift crew clock out then too). Morning must end no earlier than 11am - still a
// guess on the start side, but a manager covering the open can't be gone before mid
// even starts.
function matchFloorManagerShift(shift, cfg) {
  if (!isRealManager(shift, cfg) && !nameIn(cfg.managersInTraining, shift.name)) return null;

  const start = localHourDecimal(shift.shiftStartTime, cfg.utcOffsetHours);
  const end = localHourDecimal(shift.shiftEndTime, cfg.utcOffsetHours);
  const shiftKey = classifyStaffShift(shift, cfg);
  if (shiftKey === "night" && start >= 15.25 && start < 15.75) return { shiftKey, confidence: "confirmed" };
  if (shiftKey === "mid" && start < 9 && ((end >= 16.75 && end < 17.25) || (end >= 17.75 && end < 18.25))) return { shiftKey, confidence: "confirmed" };
  if (shiftKey === "morning" && start < 7 && end >= 11) return { shiftKey, confidence: "guess" };
  return null;
}

// "Working with today" / night-scoped checks use this - whether a shift actually overlaps
// night hours (ends after nightStartHour) rather than just being scheduled sometime that
// calendar day. Compared as real timestamps, not hour-of-day numbers, so a shift ending
// after midnight doesn't wrap around and look like it ends "early".
function worksIntoNightShift(shift, dateStr, cfg) {
  const [y, m, d] = dateStr.split("-").map(Number);
  const nightStartUTC = Date.UTC(y, m - 1, d, cfg.nightStartHour - cfg.utcOffsetHours, 0, 0);
  return new Date(shift.shiftEndTime).getTime() > nightStartUTC;
}

function teamNoteFor(teamNotes, name) {
  return teamNotes.find((p) => p.name.toLowerCase() === name.toLowerCase());
}

// "Clocked in outside normal hours" - store opens 4:30ish/closes 11:30 in practice, so a
// shift whose start or end falls in the (11:30pm, 4:30am) gap is worth a second look (bad
// data entry, or a genuine one-off). Hour-of-day is already correctly wrapped by
// localHourDecimal regardless of which calendar day the shift falls on, so this doesn't
// need the timestamp-threshold gymnastics the "remaining hours" cut-suggestion math needed -
// it's just "is this moment's time-of-day in the forbidden window", not an elapsed duration.
function isOutsideNormalHours(hourOfDay) {
  return hourOfDay > 23.5 || hourOfDay < 4.5;
}

function outsideHoursWarnings(roster, cfg) {
  return roster.filter((s) => {
    const startH = localHourDecimal(s.shiftStartTime, cfg.utcOffsetHours);
    const endH = localHourDecimal(s.shiftEndTime, cfg.utcOffsetHours);
    return isOutsideNormalHours(startH) || isOutsideNormalHours(endH);
  });
}
