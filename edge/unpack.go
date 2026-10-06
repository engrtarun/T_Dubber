package edge

// Extraction of a verified artefact into a usable tree.
//
// WHY THIS IS HERE AND NOT IN THE NOTEBOOK
// ----------------------------------------
// edge-fetch downloads and digest-verifies archives. Verifying is pointless if
// nothing then unpacks them: a 2 GB pylibs.tar.gz sitting in edge_cache is not
// a Python environment, and find_pylibs() requires an actual
// `pylibs/vllm/__init__.py` on disk. So the same tool that proved the bytes
// are the bytes we asked for is the tool that turns them into a directory --
// in Go, with no interpreter, because this has to run before the interpreter
// has its dependencies.
//
// WHY EXTRACTION IS STAGED AND THEN RENAMED
// -----------------------------------------
// An archive's members arrive in whatever order the tarball recorded them, and
// the `.tdubber_ready` marker that find_pylibs() demands is a dotfile: it is
// very often among the first entries extracted. A Kaggle session reaped
// mid-unpack would therefore leave a tree that LOOKS ready and imports nothing.
//
// So everything is unpacked into a hidden temporary directory next to the
// target and moved into place only once the archive has been read to the end.
// The marker appears atomically with the tree it vouches for, or not at all.
//
// WHAT IS REFUSED
// ---------------
// Absolute paths, `..` traversal, and any link whose target resolves outside
// the destination. Each member is checked against the destination rather than
// trusted, because the archive came off the network. This is the same
// containment rule as Resolve, applied member by member.

import (
	"archive/tar"
	"archive/zip"
	"compress/gzip"
	"errors"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"strings"
)

// ErrUnsupportedArchive is returned for a file whose extension names no
// archive format edge knows how to read. It is not a corruption error: the
// caller may have artefacts that are meant to be used as plain files.
var ErrUnsupportedArchive = errors.New("edge: unsupported archive format")

// IsArchive reports whether an artefact name denotes an archive that Unpack
// understands. The check is on the extension alone, deliberately: a file that
// claims to be a tarball in its manifest but is not one fails loudly at Unpack,
// rather than quietly being left unextracted.
func IsArchive(name string) bool {
	n := strings.ToLower(filepath.ToSlash(name))
	for _, ext := range []string{".tar.gz", ".tgz", ".tar", ".zip"} {
		if strings.HasSuffix(n, ext) {
			return true
		}
	}
	return false
}

// UnpackResult says what an extraction actually did, so the notebook can print
// a line that distinguishes "nothing to do" from "5 GB unpacked" from "three
// members were refused".
type UnpackResult struct {
	Files   int    // regular files written
	Dirs    int    // directories created
	Links   int    // symlinks recreated (all inside the destination)
	Skipped int    // members refused or ignored, with a reason in Warnings
	Bytes   int64  // bytes written
	Target  string // the directory the tree was moved into
	Warns   []string
}

// Unpack extracts a verified archive into targetDir.
//
// targetDir must exist; it is created if not. Extraction is staged in a
// temporary directory alongside targetDir and renamed into place on success,
// so a partial extraction is never visible under the real path. The temporary
// directory is always removed.
//
// The returned error is non-nil only when the archive could not be used at
// all. Individual refused members are counted in the result and listed in
// Warns -- one hostile path must not cost the other ten gigabytes, but it must
// not pass silently either.
func Unpack(archivePath, targetDir string) (*UnpackResult, error) {
	if err := os.MkdirAll(targetDir, 0o755); err != nil {
		return nil, fmt.Errorf("edge: unpack: mkdir %s: %w", targetDir, err)
	}

	staging, err := os.MkdirTemp(targetDir, ".edge-unpack-*")
	if err != nil {
		return nil, fmt.Errorf("edge: unpack: staging dir: %w", err)
	}
	// Clean up on every path; after a successful move the directory is empty
	// anyway, so this costs one RemoveAll either way.
	defer os.RemoveAll(staging)

	res := &UnpackResult{}
	lower := strings.ToLower(archivePath)
	switch {
	case strings.HasSuffix(lower, ".tar.gz"), strings.HasSuffix(lower, ".tgz"):
		err = unpackTarGz(archivePath, staging, res)
	case strings.HasSuffix(lower, ".tar"):
		err = unpackTar(archivePath, staging, res)
	case strings.HasSuffix(lower, ".zip"):
		err = unpackZip(archivePath, staging, res)
	default:
		return nil, fmt.Errorf("%w: %s", ErrUnsupportedArchive, filepath.Base(archivePath))
	}
	if err != nil {
		return nil, fmt.Errorf("edge: unpack %s: %w", filepath.Base(archivePath), err)
	}
	if res.Files == 0 && res.Dirs == 0 && res.Links == 0 {
		// An archive with no members is not a usable cache and would leave the
		// caller believing the tree is in place.
		return nil, fmt.Errorf("edge: unpack %s: archive contained no files",
			filepath.Base(archivePath))
	}

	if err := promote(staging, targetDir, res); err != nil {
		return nil, err
	}
	res.Target = targetDir
	return res, nil
}

// promote moves the staged tree into its final location.
//
// Each top-level entry is renamed individually, which is atomic within one
// filesystem. An existing destination is removed first: os.Rename over a
// non-empty directory fails on Linux, and a stale half-tree left by an older
// run is exactly what must not survive.
func promote(staging, targetDir string, res *UnpackResult) error {
	top, err := os.ReadDir(staging)
	if err != nil {
		return fmt.Errorf("edge: unpack: reading staging dir: %w", err)
	}
	for _, ent := range top {
		src := filepath.Join(staging, ent.Name())
		dst := filepath.Join(targetDir, ent.Name())
		if _, statErr := os.Lstat(dst); statErr == nil {
			if rmErr := os.RemoveAll(dst); rmErr != nil {
				return fmt.Errorf("edge: unpack: replacing %s: %w", dst, rmErr)
			}
		}
		if err := os.Rename(src, dst); err != nil {
			return fmt.Errorf("edge: unpack: moving %s into place: %w", ent.Name(), err)
		}
	}
	return nil
}

// inside reports whether candidate is dest or lives beneath it.
//
// Both arguments must already be absolute and cleaned. The check is done with
// filepath.Rel so it cannot be fooled by a prefix that merely shares
// characters -- `/data/artefacts-evil` is not inside `/data/artefacts`.
func inside(dest, candidate string) bool {
	rel, err := filepath.Rel(dest, candidate)
	if err != nil {
		return false
	}
	return rel == "." || (!strings.HasPrefix(rel, ".."+string(filepath.Separator)) && rel != ".." && !filepath.IsAbs(rel))
}

// memberPath joins a member name onto dest, refusing anything that would land
// outside it. Names in tar and zip are slash-separated and may be absolute on
// a hostile archive, so both are normalised before the containment test.
func memberPath(dest, name string) (string, error) {
	name = strings.ReplaceAll(name, `\`, "/")
	if name == "" || name == "." {
		return "", errors.New("empty member name")
	}
	if strings.ContainsRune(name, 0) {
		return "", errors.New("member name contains NUL")
	}
	// Absolute names are refused rather than made relative.
	//
	// Stripping the leading "/" would be *safe* -- the member would land inside
	// the destination -- but it would silently change where the archive says a
	// file lives, and a tree installed at a path other than the one it was
	// built for fails later as a missing module instead of as a refused
	// archive. An absolute member means the archive was built by something
	// that tars a filesystem path; say so now.
	if strings.HasPrefix(name, "/") || filepath.IsAbs(filepath.FromSlash(name)) || hasDrive(name) {
		return "", fmt.Errorf("member %q is an absolute path", name)
	}
	full := filepath.Join(dest, filepath.FromSlash(name))
	if !inside(dest, full) {
		return "", fmt.Errorf("member %q escapes the destination", name)
	}
	return full, nil
}

// hasDrive reports whether a slash-normalised name starts with a Windows drive
// specifier. filepath.IsAbs only recognises the current OS's syntax, so on
// Linux "C:/Windows/x" would otherwise be treated as a relative path.
func hasDrive(name string) bool {
	return len(name) >= 2 && name[1] == ':' &&
		((name[0] >= 'a' && name[0] <= 'z') || (name[0] >= 'A' && name[0] <= 'Z'))
}

// memberDir is memberPath applied to a member's parent directory, for the
// symlink case where a link's target has to be resolved against the directory
// the link itself lives in. A member at the archive root has the destination
// as its parent, and "." is not a name memberPath accepts.
func memberDir(dest, name string) (string, error) {
	dir := filepath.ToSlash(filepath.Dir(filepath.FromSlash(name)))
	if dir == "." || dir == "" {
		return dest, nil
	}
	return memberPath(dest, dir)
}

func writeDir(path string) error {
	return os.MkdirAll(path, 0o755)
}

func writeFile(path string, mode os.FileMode, r io.Reader) (int64, error) {
	if err := os.MkdirAll(filepath.Dir(path), 0o755); err != nil {
		return 0, err
	}
	// 0o600 until the copy is done: a partially written file is never readable
	// by anything else, and the mode is corrected afterwards.
	f, err := os.OpenFile(path, os.O_CREATE|os.O_TRUNC|os.O_WRONLY, 0o600)
	if err != nil {
		return 0, err
	}
	n, copyErr := io.Copy(f, r)
	closeErr := f.Close()
	if copyErr != nil {
		return n, copyErr
	}
	if closeErr != nil {
		return n, closeErr
	}
	// exec bit comes from the archive; write bit is implied. A mode with no
	// read bit at all would produce a tree the owner cannot import from.
	perm := mode.Perm()
	if perm&0o400 == 0 {
		perm |= 0o600
	}
	if err := os.Chmod(path, perm); err != nil {
		return n, err
	}
	return n, nil
}

// safeLinkMode is the permission used for a recreated symlink (unused on
// Windows, where symlink creation may be refused outright).
const safeLinkMode os.FileMode = 0o777

func writeLink(base, relTarget, linkPath string) error {
	if err := os.MkdirAll(filepath.Dir(linkPath), 0o755); err != nil {
		return err
	}
	resolved := filepath.Join(filepath.Dir(linkPath), filepath.FromSlash(relTarget))
	if !inside(base, resolved) {
		return fmt.Errorf("symlink target escapes the destination")
	}
	if err := os.Remove(linkPath); err != nil && !os.IsNotExist(err) {
		return err
	}
	return os.Symlink(filepath.FromSlash(relTarget), linkPath)
}

func unpackTarGz(path, dest string, res *UnpackResult) error {
	f, err := os.Open(path)
	if err != nil {
		return err
	}
	defer f.Close()
	gz, err := gzip.NewReader(f)
	if err != nil {
		return fmt.Errorf("gzip: %w", err)
	}
	defer gz.Close()
	return readTar(tar.NewReader(gz), dest, res)
}

func unpackTar(path, dest string, res *UnpackResult) error {
	f, err := os.Open(path)
	if err != nil {
		return err
	}
	defer f.Close()
	return readTar(tar.NewReader(f), dest, res)
}

func readTar(tr *tar.Reader, dest string, res *UnpackResult) error {
	for {
		hdr, err := tr.Next()
		if err == io.EOF {
			return nil
		}
		if err != nil {
			return fmt.Errorf("tar: %w", err)
		}
		name := hdr.Name
		switch hdr.Typeflag {
		case tar.TypeDir:
			p, err := memberPath(dest, name)
			if err != nil {
				res.refuse(name, err)
				continue
			}
			if err := writeDir(p); err != nil {
				return err
			}
			res.Dirs++
		case tar.TypeReg:
			p, err := memberPath(dest, name)
			if err != nil {
				res.refuse(name, err)
				continue
			}
			n, err := writeFile(p, os.FileMode(hdr.Mode), tr)
			if err != nil {
				return fmt.Errorf("writing %s: %w", name, err)
			}
			res.Files++
			res.Bytes += n
		case tar.TypeSymlink:
			// A link is a pointer out of the archive: it is the one member
			// type that can write OUTSIDE the destination without any of its
			// own bytes being wrong. Resolved against the link's directory and
			// re-checked, an escaping link is refused rather than created.
			base, err := memberDir(dest, name)
			if err != nil {
				res.refuse(name, err)
				continue
			}
			p, err := memberPath(dest, name)
			if err != nil {
				res.refuse(name, err)
				continue
			}
			if err := writeLink(base, hdr.Linkname, p); err != nil {
				res.refuse(name, err)
				continue
			}
			res.Links++
		case tar.TypeXGlobalHeader, tar.TypeXHeader, tar.TypeGNULongName, tar.TypeGNULongLink:
			// Metadata the tar reader already consumed. Not a member.
		default:
			// Devices, FIFOs, hardlinks: not something a Python tree needs,
			// and each is a way for an archive to be more than it says.
			res.refuse(name, fmt.Errorf("unsupported tar member type %q", hdr.Typeflag))
		}
	}
}

func unpackZip(path, dest string, res *UnpackResult) error {
	zr, err := zip.OpenReader(path)
	if err != nil {
		return fmt.Errorf("zip: %w", err)
	}
	defer zr.Close()

	for _, zf := range zr.File {
		name := zf.Name
		if zf.FileInfo().IsDir() {
			p, err := memberPath(dest, name)
			if err != nil {
				res.refuse(name, err)
				continue
			}
			if err := writeDir(p); err != nil {
				return err
			}
			res.Dirs++
			continue
		}
		p, err := memberPath(dest, name)
		if err != nil {
			res.refuse(name, err)
			continue
		}
		mode := zf.Mode()
		if mode&os.ModeSymlink != 0 {
			rc, err := zf.Open()
			if err != nil {
				res.refuse(name, err)
				continue
			}
			target, err := io.ReadAll(io.LimitReader(rc, 4096))
			rc.Close()
			if err != nil {
				res.refuse(name, err)
				continue
			}
			base, err := memberDir(dest, name)
			if err != nil {
				res.refuse(name, err)
				continue
			}
			if err := writeLink(base, string(target), p); err != nil {
				res.refuse(name, err)
				continue
			}
			res.Links++
			continue
		}
		rc, err := zf.Open()
		if err != nil {
			return fmt.Errorf("opening %s: %w", name, err)
		}
		// The zip reader enforces the declared size and CRC itself, so the body
		// goes straight to the file: wrapping it in a second limit would only
		// truncate a legitimate stream whose header was wrong.
		n, err := writeFile(p, mode.Perm(), rc)
		rc.Close()
		if err != nil {
			return fmt.Errorf("writing %s: %w", name, err)
		}
		res.Files++
		res.Bytes += n
	}
	return nil
}

// refuse records a member that was not extracted, keeping the reason. The
// count matters more than the names: the caller prints totals, and the log
// stays readable when an archive carries thousands of members.
func (r *UnpackResult) refuse(name string, why error) {
	r.Skipped++
	if len(r.Warns) < 20 {
		r.Warns = append(r.Warns, fmt.Sprintf("%s: %v", name, why))
	}
}
