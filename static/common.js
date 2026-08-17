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
