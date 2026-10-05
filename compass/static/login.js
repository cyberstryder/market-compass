document.querySelector('#login').addEventListener('submit',async e=>{
 e.preventDefault();const out=document.querySelector('#error');out.textContent='';
 try{const r=await fetch('/login',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({password:document.querySelector('#password').value})});
 if(!r.ok){const d=await r.json();throw new Error(d.detail||'Unable to sign in');}location.href='/detailed';}catch(x){out.textContent=x.message;}
});