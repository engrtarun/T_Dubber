import { redirect } from "next/navigation";

/**
 * Root only redirects — the drive UI lives in `public/drive-ui/`.
 *
 * The capture is a multi-page site whose HTML uses RELATIVE links
 * (`assets/...`, `pages/...`), so it must be opened at a path that ends
 * in a slash or a filename. `index.html` does exactly that, and it is
 * what the capture itself was built to be opened as. Pointing `/` here
 * means there is exactly ONE copy of the UI in this app: the untouched
 * capture under `public/drive-ui/`.
 */
export default function Home() {
  redirect("/drive-ui/index.html");
}