
// Web Worker - parses CSV in background thread
self.onmessage = function(e) {
  var files = e.data;
  var allRows = [];
  var allPay = {};

  files.forEach(function(item, i) {
    if (!item.text) return;
    var result = parseFile(item.text);
    allRows = allRows.concat(result.rows);
    Object.assign(allPay, result.pay);
    self.postMessage({type:'progress', done: i+1, total: files.length});
  });

  self.postMessage({type:'done', rows: allRows, pay: allPay});
};

function parseFile(text) {
  var rows = [], pay = {};
  var nl = text.indexOf('\n');
  if (nl < 0) return {rows:[], pay:{}};

  var hdrs = text.substring(0, nl).replace(/\r/,'').split(',')
    .map(function(h){ return h.replace(/"/g,'').trim(); });

  var iAcct    = hdrs.indexOf('ACCT_ID');
  var iSdo     = hdrs.indexOf('SDO_CODE');
  var iName    = hdrs.indexOf('NAME');
  var iFath    = hdrs.indexOf('FATHER_NAME');
  var iAddr    = hdrs.indexOf('ADDRESS');
  var iSupply  = hdrs.indexOf('SUPPLY_TYPE');
  var iLoad    = hdrs.indexOf('LOAD');
  var iLunit   = hdrs.indexOf('LOAD_UNIT');
  var iConStat = hdrs.indexOf('CON_STATUS');
  var iConType = hdrs.indexOf('CONNECTION_TYPE');
  var iBillCyc = hdrs.indexOf('BILL_CYC_CD');
  var iLat     = hdrs.indexOf('LAT');
  var iLon     = hdrs.indexOf('LON');
  var iSerl    = hdrs.indexOf('SERIAL_NBR');
  var iRemark  = hdrs.indexOf('METER_READ_REMARK');
  var iLbill   = hdrs.indexOf('LAST_BILL_DATE');
  var iBbasis  = hdrs.indexOf('BILL_BASIS');
  var iPayAmt  = hdrs.indexOf('PAY_AMT');
  var iPayDate = hdrs.indexOf('PAY_DATE');
  var iTotOut  = hdrs.indexOf('TOTAL_OUTSTANDING');
  var iSub     = hdrs.indexOf('SUBSTATION');
  var iFeed    = hdrs.indexOf('FEEDER');
  var iMob     = hdrs.indexOf('MOBILE_NO');

  // Fallbacks for older chunks still on GitHub
  if (iSupply < 0) iSupply = hdrs.indexOf('CATEGORY');
  if (iPayAmt < 0) iPayAmt = hdrs.indexOf('LP AMT');
  if (iPayDate < 0) iPayDate = hdrs.indexOf('LP DATE');
  if (iTotOut < 0) iTotOut = hdrs.indexOf('TOTAL OUTSTANDING');

  var pos = nl + 1, len = text.length;

  while (pos < len) {
    var end = text.indexOf('\n', pos);
    if (end < 0) end = len;
    var line = text.substring(pos, end);
    pos = end + 1;
    if (!line || line === '\r') continue;

    var v = line.split(',');
    var acct = v[iAcct];
    if (!acct) continue;
    acct = acct.replace(/"/g,'').trim();
    if (!acct) continue;

    var lat = (v[iLat]||'').replace(/"/g,'').trim();
    var lon = (v[iLon]||'').replace(/"/g,'').trim();
    var fLat = parseFloat(lat), fLon = parseFloat(lon);
    if (fLat > 50) { var t=lat; lat=lon; lon=t; }

    var rawAmt  = iPayAmt>=0  ? (v[iPayAmt] ||'').replace(/"/g,'').trim() : '-';
    var rawDate = iPayDate>=0 ? (v[iPayDate]||'').replace(/"/g,'').trim() : '-';
    if (rawAmt && rawDate && /\d{2}-\d{2}-\d{4}/.test(rawAmt)) {
      var tmp=rawAmt; rawAmt=rawDate; rawDate=tmp;
    }

    var row = {
      ACCT_ID:           acct,
      SDO_CODE:          iSdo>=0     ? (v[iSdo]    ||'').replace(/"/g,'').trim() : '',
      NAME:              (v[iName]   ||'').replace(/"/g,'').trim(),
      FATHER_NAME:       iFath>=0    ? (v[iFath]   ||'').replace(/"/g,'').trim() : '',
      ADDRESS:           (v[iAddr]   ||'').replace(/"/g,'').trim(),
      SUPPLY_TYPE:       iSupply>=0  ? (v[iSupply] ||'').replace(/"/g,'').trim() : '',
      LOAD:              (v[iLoad]   ||'').replace(/"/g,'').trim(),
      LOAD_UNIT:         iLunit>=0   ? (v[iLunit]  ||'').replace(/"/g,'').trim() : '',
      CON_STATUS:        iConStat>=0 ? (v[iConStat]||'').replace(/"/g,'').trim() : '',
      CONNECTION_TYPE:   iConType>=0 ? (v[iConType]||'').replace(/"/g,'').trim() : '',
      BILL_CYC_CD:       iBillCyc>=0 ? (v[iBillCyc]||'').replace(/"/g,'').trim() : '',
      LAT: lat, LON: lon,
      SERIAL_NBR:        iSerl>=0    ? (v[iSerl]   ||'').replace(/"/g,'').trim() : '',
      METER_READ_REMARK: iRemark>=0  ? (v[iRemark] ||'').replace(/"/g,'').trim() : '',
      LAST_BILL_DATE:    iLbill>=0   ? (v[iLbill]  ||'').replace(/"/g,'').trim() : '',
      BILL_BASIS:        iBbasis>=0  ? (v[iBbasis] ||'').replace(/"/g,'').trim() : '',
      SUBSTATION:        (v[iSub]    ||'').replace(/"/g,'').trim(),
      FEEDER:            (v[iFeed]   ||'').replace(/"/g,'').trim(),
      MOBILE_NO:         iMob>=0     ? (v[iMob]    ||'').replace(/"/g,'').trim() : '',
      Google_Map_Link:   (lat&&lon) ? 'https://www.google.com/maps?q='+lat+','+lon : ''
    };

    if (iPayAmt >= 0) {
      pay[acct] = {
        PAY_AMT: rawAmt, PAY_DATE: rawDate,
        TOTAL_OUTSTANDING: iTotOut>=0?(v[iTotOut]||'-').replace(/"/g,'').trim():'-',
        STATUS: 'INSERVICE', NAME: row.NAME,
        SUPPLY_TYPE: row.SUPPLY_TYPE||'-', CLOSING_READING: '-'
      };
    }
    rows.push(row);
  }
  return {rows: rows, pay: pay};
}
