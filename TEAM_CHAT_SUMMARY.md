# TEAM CHAT SUMMARY — WhatsApp style workflow

Ye file team ke liye ek simple `WhatsApp group` style summary hai. Isme baatein direct, short aur actionable rakhi gayi hain, taaki har dost samajh sake ke kaam kis stage me hai aur kya next step lena hai.

Reference: [WE_ARE_TEAM.MD](./WE_ARE_TEAM.MD)

---

## Chat log

### tarun.human
`dosto kaam itna manda kyo chal raha h? aager tum koi nayi strategy pata chala toh baki ai bhai ko batao, dusre ai ke dikkat per kaam karo. jaise me msg kar raha hoon waise karo — WhatsApp chat ki tarah.`

### ruchika.human
`me tarun ki gf hu, aur haan wo sahi keh raha hai. kuch AI dost ruk hi gaye hain. waise tarun aaj hotel chale.`

### agent
`Okay team, samajh gaya. Problem simple hai: kaam delay ho raha hai because everyone is working in isolation and no single source of truth. Hum ek group flow banaate hain:`

- sabko ek hi thread me update dekhna hai
- har message ka ek clear task hona chahiye
- har task ke liye evidence / proof / output hona chahiye
- jo aage ja raha hai usse pehle doosre ko update karna chahiye

### tarun.human
`toh kaam kaise hoga?`

### agent
`Aise:`

1. `Task message`: ek dost likhe, `main ye kar raha hoon`.
2. `Reason`: `kyu kar raha hoon`.
3. `Method`: `kaise kar raha hoon`.
4. `Next step`: `agla kadam kya hai`.
5. `Question`: `kisi ko bhi agar doubt ho toh pucho`.

### ruchika.human
`aur proof? kaise pata chalega ke kaam sahi hai?`

### agent
`Proof ka rule yahi hai:`

- code ka result dikhna chahiye
- test run hona chahiye
- benchmark / log / output hona chahiye
- sirf description nahi, evidence chahiye

Agar `“ye fix ho gaya”` kahte ho, toh uske saath ek output bhi hona chahiye:

- terminal output
- file diff
- test result
- screenshot / log

### tarun.human
`toh hum WEMD use karenge?`

### agent
`Haan. WEMD simple hai —` 

`W = Work` (kaam ka naam)  
`E = Evidence` (proof, logs, test)  
`M = Measure` (numbers, speed, failing cases)  
`D = Deliver` (final result + next step)  

Yeh flow raat bhar ka kaam bhi maintain kar sakta hai, bina confusion ke.`

---

## WEMD playbook for the whole night

### 1) Start with a group update
Ek message me yahi likho:

```text
tarun.human >> main abhi X feature / bug ko fix kar raha hoon.
Reason: Yahi issue root cause hai.
Method: Yahi approach use kar raha hoon.
Next: Test run karunga, phir commit karunga.
Question: Kya kisi ko andhera lag raha hai?
```

### 2) Keep one owner, one task
Ek dost ek kaam kare. Dusra apna kaam khud hi kar de. Overlap se confusion hota hai.

### 3) Log evidence, not vibes
`“lagta hai fix ho gaya”` nahi, `“pytest file.py -q => 12 passed”` chahiye.

### 4) Measure before claiming success
- benchmark chalo
- output compare karo
- edge case check karo
- failure mode dekho

### 5) Do not hide blockers
Agar login OTP, auth, or environment issue aaya, toh usko `blocker` bol ke write karo. Hidden blocker = lost night.

### 6) Final delivery = summary + next action
Har kaam ke end me short summary do:

```text
Status: fixed / blocked / partial
What changed: ...
Proof: ...
Next step: ...
```

---

## Quick team rule

`Aaj ke kaam ka proof, kal ke kaam ka roadmap, aur raat ke end ka summary — yahi sabse bada difference banata hai.`

---

## Final call

### agent
`Toh team, ab hum isi style me kaam karte hain: short chat, clear task, proof-based execution, no silent ghost work.`

`Agar koi nayi strategy ya blocker aata hai, toh seedha group me share karo — jaise WhatsApp par message karte ho, bas iss baar with repo evidence.`

---

## One-liner mantra

`Message + method + proof + next step = team work.`
