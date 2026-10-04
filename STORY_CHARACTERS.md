# T_Dubber Project Story Characters 🎬

Yeh file isliye banayi gayi hai taaki aap kisi bhi AI (ya non-tech person) ko apne system ka architecture aur flow aasaani se "Kahani" ke roop mein samjha sakein.

## The Core Team (Characters)

1. **The Front Desk / Receptionist (`app.py` & Gradio UI)**
   - **Kaam:** User se milna, file lena (drag & drop), aur instructions (jaise Telegram channel) collect karna.
   - **Role:** Yeh seedha user se interact karti hai. Iska kaam sirf orders lena aur final delivery ka message user ko dikhana hai.

2. **The Delivery Manager (`telegram_uploader.py`)**
   - **Kaam:** Poore upload process ko orchestrate (manage) karna.
   - **Role:** Ye dekhta hai ki file ko chote chunks mein kaatna hai ya nahi, progress bar update karwana hai, aur rules enforce karne hain. Pehle yeh khud bhi kaam (Telethon se upload) karta tha, par ab isne kasam kha li hai ki saara heavy-lifting Go ke engine ko hi dega.

3. **The Scout / Traffic Police (`auto_tuner.py`)**
   - **Kaam:** Telegram servers tak internet ki speed check karna.
   - **Role:** Delivery shuru hone se pehle yeh ek quick test karta hai aur batata hai ki 2 trucks bhejein ya 4 trucks, taaki speed fast rahe aur Telegram se "FLOOD_WAIT" ka chalaan (error) na aaye.

4. **The Hawaldar / Intelligence (`db.py` - SQLite)**
   - **Kaam:** Har chhoti-badi baat ka record rakhna.
   - **Role:** Yeh police constable hai jo hidden tareeke se system mein baitha hai. Kaunsi file aayi (link se ya local device se), kab upload hui, speed kya thi, size kitna tha - yeh sab note karta hai. Telegram ke paas apna record hai, par wahan dhoondhna slow hai aur jyada queries par limit lagti hai. Hawaldar offline, instant aur detail report deta hai. Duplicate uploads ko turant rokta hai.

5. **The Smart Conductor (`run_go.ps1`)**
   - **Kaam:** Engine ko dhoondhna aur bina kisi pareshani ke chalu karna. System health check karna.
   - **Role:** Yeh conductor truck mein baithta hai. Ise Windows OS ki samajh hai. Yeh check karta hai ki system ki storage khali hai ya nahi, RAM/CPU theek hai, internet chal raha hai ya nahi, aur Go engine kahan rakha hai. Sab check karne ke baad yeh Baahubali engine ko start signal deta hai aur safar ki live reporting (progress) wapas bhejta hai. (Code mein Iske paas sabse zyada functions aur line of codes honge).

6. **The Secure Translator / Bridge (`tgup_bridge.py`)**
   - **Kaam:** Python aur Go ke beech baat karwana securely.
   - **Role:** Yeh translator hai. Sath hi yeh Security Incharge bhi hai. Telegram ke API passwords yeh openly pass nahi hone deta, balki chupke se (stdin pipe ke zariye) Conductor/Engine ke kaano mein whisper karta hai, taaki hackers ya Task Manager inhe track na kar sakein.

7. **The Muscle / Baahubali Engine (`main.go`, `commands.go`, `upload.go`)**
   - **Kaam:** Bhari saamaan (1KB se 1TB tak ki files) ko Telegram tak fast deliver karna.
   - **Role:** Yeh Go lang se bana Baahubali truck hai. Ise order milte hi yeh apna 4-haath wala parallel system chalata hai. Yeh itna fast hai ki network ka full use karta hai bina rukawat ke. Aur ab chaahe thumbnail attach karna ho ya caption dena ho, saara kaam isi ko karna hai.

## 🚀 Future Scope (Naye Characters Ki Entry)

8. **The Fancy Interior Designer (TypeScript/React/NextJS)**
   - **Kaha Aayega?** Jab humein `app.py` (Gradio) ka basic UI change karke ek premium Web App (Dashboard) banana hoga.
   - **Role:** Yeh front desk ko ek luxury 5-star hotel ke reception mein badal dega. Smooth animations, drag & drop features, dark mode aur instant clicks.

9. **The Sniper / Ninja (Rust)**
   - **Kaha Aayega?** Jab humein files ko upload karne se pehle process karna ho (jaise 4K video ko 1080p mein compress karna, watermark lagana, ya video dubbing karna) aur hum chahte hain ki CPU kam se kam use ho.
   - **Role:** Rust ek ninja hai. Yeh memory safe hai aur video encoding jaisi bhari cheezon ko Go aur Python se bhi zyada tightly handle kar sakta hai. Yeh background mein aayega, video ko silently compress karega aur Baahubali Go truck mein load kar dega.
