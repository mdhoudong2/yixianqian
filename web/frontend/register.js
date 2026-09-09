/* 一线牵 H5 注册页逻辑：字段由 register.meta.js 驱动，严格对齐飞书表单「一线牵-注册（新）」 */
const { createApp } = Vue;

function getCookie(name){
  const m = document.cookie.match(new RegExp("(?:^|; )" + name + "=([^;]*)"));
  return m ? decodeURIComponent(m[1]) : "";
}
// —— 行政区划短名：与表单示例风格一致（广东广州 / 深圳南山）——
const PROV_FULL = { "广西壮族自治区": "广西", "新疆维吾尔自治区": "新疆", "内蒙古自治区": "内蒙古",
  "宁夏回族自治区": "宁夏", "西藏自治区": "西藏", "香港特别行政区": "香港", "澳门特别行政区": "澳门" };
function shortProv(n){ if(PROV_FULL[n]) return PROV_FULL[n]; return (n||"").replace(/(省|市)$/, ""); }
function shortCity(n){ return (n||"").replace(/市$/, ""); }
function shortDist(n){ return (n||"").replace(/(区|县|市)$/, ""); }

const app = createApp({
  data(){
    return {
      meta: window.REGISTER_META || [],
      AREA: window.AREA_DATA || {},
      stage: "loading",        // loading | needfeishu | registered | form | done
      loginErr: "",
      dbg: [],
      form: {},
      photos: [],
      submitting: false,
      // 级联选择
      areaShow: false,
      areaMeta: null,
      areaStep: 0,
      areaSel: { prov: "", city: "", dist: "" },
      csrf: getCookie("yxq_csrf") || "",
    };
  },
  computed: {
    areaOptions(){
      if(this.areaStep === 0) return Object.keys(this.AREA);
      if(this.areaStep === 1) return Object.keys(this.AREA[this.areaSel.prov] || {});
      return (this.AREA[this.areaSel.prov] || {})[this.areaSel.city] || [];
    },
    areaTitle(){ return this.areaMeta ? this.areaMeta.title : "选择地区"; },
    areaCrumb(){
      const s = this.areaSel;
      return [s.prov, s.city, this.areaStep===2?s.dist:""].filter(Boolean).join(" / ") || "请选择";
    },
  },
  mounted(){
    this.log("页面挂载, UA含飞书=" + this.isInFeishu());
    this.initInviter();
    this.bootLogin();
  },
  methods: {
    log(m){ const t=new Date().toLocaleTimeString(); this.dbg.push(t+" "+m); console.log("[reg]",m); },
    isInFeishu(){ return /lark|feishu/i.test(navigator.userAgent); },
    async api(url, opt={}){
      const headers = Object.assign({ "X-Requested-With": "XMLHttpRequest" }, opt.headers || {});
      if(this.csrf) headers["X-CSRF-Token"] = this.csrf;
      const resp = await fetch(url, Object.assign({ credentials: "same-origin" }, opt, { headers }));
      let data = {};
      try { data = await resp.json(); } catch(e){}
      if(!resp.ok) throw new Error(data.error || ("请求失败(" + resp.status + ")"));
      return data;
    },
    // —— 登录（飞书内静默），带 8 秒硬超时，避免永久转圈 ——
    getSilentCode(){
      const task = new Promise((resolve)=>{
        let waited = 0;
        const tryGet = ()=>{
          const tt = window.tt || window.h5sdk;
          if(!tt){ if(waited<4000){ waited+=200; setTimeout(tryGet,200); return; } this.log("JSSDK未加载"); resolve({code:null,error:"JSSDK未加载"}); return; }
          this.log("JSSDK就绪, 有requestAuthCode="+(typeof tt.requestAuthCode)+", requestAccess="+(typeof tt.requestAccess));
          const done=(c,e)=>{ this.log("免登返回 code="+(c?"有":"无")+(e?(" err="+e):"")); resolve({code:c,error:e}); };
          if(typeof tt.requestAuthCode === "function"){
            tt.requestAuthCode({
              appId: window.FEISHU_APP_ID,
              success: r=>done(r&&r.code?r.code:null,null),
              fail: err=>{
                const es = JSON.stringify(err);
                if(typeof tt.requestAccess === "function"){
                  tt.requestAccess({ appID: window.FEISHU_APP_ID, scopeList:[],
                    success:r2=>done(r2&&r2.code?r2.code:null,null),
                    fail:e2=>done(null,"requestAccess:"+JSON.stringify(e2)) });
                } else done(null,"requestAuthCode:"+es);
              }
            });
          } else if(typeof tt.requestAccess === "function"){
            tt.requestAccess({ appID: window.FEISHU_APP_ID, scopeList:[],
              success:r=>done(r&&r.code?r.code:null,null),
              fail:e=>done(null,"requestAccess:"+JSON.stringify(e)) });
          } else done(null,"无可用免登API");
        };
        tryGet();
      });
      const timeout = new Promise(res=>setTimeout(()=>res({code:null,error:"免登超时(8s)"}),8000));
      return Promise.race([task, timeout]);
    },
    async bootLogin(){
      this.stage="loading";
      if(!this.isInFeishu()){ this.loginErr="当前不在飞书环境"; this.stage="needfeishu"; return; }
      const r = await this.getSilentCode();
      if(!r || !r.code){ this.loginErr=(r&&r.error)||"未拿到免登code"; this.stage="needfeishu"; return; }
      try{
        this.log("用code换会话 appId="+window.FEISHU_APP_ID);
        const d = await this.api("/api/auth/feishu_register?code=" + r.code);
        this.log("换会话成功 registered="+d.registered);
        this.stage = d.registered ? "registered" : "form";
      }catch(e){ this.loginErr="换会话失败:"+e.message; this.stage="needfeishu"; this.log(this.loginErr); }
    },
    retry(){ this.log("手动重试"); this.bootLogin(); },
    // —— 邀请人预填 ——
    initInviter(){
      const q = new URLSearchParams(location.search);
      const inv = (q.get("inviter") || q.get("prefill_邀请人ID") || q.get("u") || "").trim().toUpperCase();
      if(inv){ this.form["邀请人ID"]=inv; this.log("预填邀请人 "+inv); }
    },
    // —— 选择交互 ——
    pickSingle(m, o){ this.form[m.field] = o; },
    togglePromise(m){ this.form[m.field] = m.options[0]; },
    toggleMulti(m, o){
      const arr = this.form[m.field] || [];
      const i = arr.indexOf(o);
      if(i>=0) arr.splice(i,1); else arr.push(o);
      this.form[m.field] = arr.slice();
    },
    // —— 省市区级联 ——
    areaPlaceholder(m){
      return m.levels.indexOf("dist")>=0
        ? (m.levels.indexOf("prov")>=0 ? "选择省 / 市 / 区" : "选择市 / 区")
        : "选择省 / 市";
    },
    openArea(m){
      this.areaMeta = m; this.areaStep = 0;
      this.areaSel = { prov:"", city:"", dist:"" };
      this.areaShow = true;
    },
    areaBack(){ if(this.areaStep>0) this.areaStep--; },
    chooseArea(opt){
      if(this.areaStep===0){ this.areaSel.prov=opt; this.areaSel.city=""; this.areaSel.dist=""; this.areaStep=1; return; }
      if(this.areaStep===1){
        this.areaSel.city=opt; this.areaSel.dist="";
        const needDist = this.areaMeta.levels.indexOf("dist")>=0;
        const dists = (this.AREA[this.areaSel.prov]||{})[opt]||[];
        if(needDist && dists.length){ this.areaStep=2; return; }
        this.finishArea(); return;
      }
      this.areaSel.dist=opt; this.finishArea();
    },
    finishArea(){
      const lv = this.areaMeta.levels, s=this.areaSel, parts=[];
      if(lv.indexOf("prov")>=0) parts.push(shortProv(s.prov));
      if(lv.indexOf("city")>=0) parts.push(shortCity(s.city));
      if(lv.indexOf("dist")>=0 && s.dist) parts.push(shortDist(s.dist));
      const out=[]; parts.forEach(x=>{ if(x && out[out.length-1]!==x) out.push(x); });
      this.form[this.areaMeta.field] = out.join(" ");
      this.areaShow=false;
    },
    // —— 照片 ——
    onPickPhotos(e){
      const files = Array.from(e.target.files || []);
      e.target.value = "";
      files.forEach(file=>{
        if(this.photos.length>=9) return;
        const item = { localUrl: URL.createObjectURL(file), token:"", status:"uploading" };
        this.photos.push(item);
        this.uploadPhoto(file, item);
      });
    },
    async uploadPhoto(file, item){
      try{
        const fd = new FormData(); fd.append("file", file, file.name||"photo.jpg");
        const d = await this.api("/api/register/photo", { method:"POST", body:fd });
        item.token = d.file_token; item.status = "done";
      }catch(err){ item.status="fail"; vant.showToast("图片上传失败：" + err.message); }
    },
    removePhoto(i){
      const p=this.photos[i]; if(p&&p.localUrl) URL.revokeObjectURL(p.localUrl);
      this.photos.splice(i,1);
    },
    // —— 提交 ——
    validate(){
      for(const m of this.meta){
        if(m.type===17){
          const done = this.photos.filter(p=>p.status==="done");
          if(this.photos.some(p=>p.status==="uploading")){ vant.showToast("照片还在上传，请稍候"); return false; }
          if(done.length<3 || done.length>9){ vant.showToast("请上传 3-9 张个人照片"); return false; }
          continue;
        }
        if(!m.required) continue;
        if(m.type===4){ if(!(this.form[m.field]||[]).length){ vant.showToast("请填写："+m.title); return false; } }
        else{
          const v=(this.form[m.field]===undefined?"":this.form[m.field]);
          if(typeof v==="string" && !v.trim()){ vant.showToast("请填写："+m.title); return false; }
          if(!v){ vant.showToast("请填写："+m.title); return false; }
        }
      }
      return true;
    },
    async submit(){
      if(this.submitting) return;
      if(!this.validate()) return;
      const payload = {};
      for(const m of this.meta){
        if(m.type===17){ payload[m.field] = this.photos.filter(p=>p.token).map(p=>p.token); continue; }
        let v = this.form[m.field];
        if(typeof v==="string") v=v.trim();
        if(v===undefined || v==="" || (Array.isArray(v)&&!v.length)) continue;
        payload[m.field] = v;
      }
      this.submitting=true;
      try{
        await this.api("/api/register", {
          method:"POST",
          headers:{ "Content-Type":"application/json" },
          body: JSON.stringify(payload),
        });
        this.stage="done";
        window.scrollTo(0,0);
      }catch(e){ vant.showToast(e.message || "提交失败，请重试"); }
      finally{ this.submitting=false; }
    },
  },
});
app.config.errorHandler = (err)=>{ try{ console.error(err); document.getElementById("boot-err").style.display="block"; document.getElementById("boot-err").textContent="脚本错误："+(err&&err.message||err); }catch(e){} };
window.addEventListener("error", e=>{ try{ const d=document.getElementById("boot-err"); if(d){ d.style.display="block"; d.textContent="资源/脚本错误："+(e.message||"")+" @"+(e.filename||""); } }catch(x){} });
app.use(vant).mount("#app");
