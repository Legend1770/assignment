
import argparse
import json
import urllib.error
import urllib.parse
import urllib.request

from packaging.requirements import Requirement
from packaging.specifiers import SpecifierSet
from packaging.version import Version, InvalidVersion


TARGET_PYTHON = Version("3.11")

PYPI_PROJECT_API = "https://pypi.org/pypi/{}/json"
PYPI_VERSION_API = "https://pypi.org/pypi/{}/{}/json"


# --------------------------------------------------
# PYPI API
# --------------------------------------------------

def fetch_pypi_data(url):
    """Fetch package information from PyPI."""

    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": "Python-Dependency-Validator/1.0"
        }
    )

    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            return json.loads(response.read().decode("utf-8"))

    except urllib.error.HTTPError as error:
        if error.code == 404:
            return None
        raise RuntimeError(f"PyPI HTTP error: {error.code}")

    except (urllib.error.URLError, TimeoutError) as error:
        raise RuntimeError(f"PyPI connection failed: {error}")


def get_package_metadata(package_name):
    encoded_name = urllib.parse.quote(package_name, safe="")
    url = PYPI_PROJECT_API.format(encoded_name)

    return fetch_pypi_data(url)


def get_version_metadata(package_name, version):
    encoded_name = urllib.parse.quote(package_name, safe="")
    encoded_version = urllib.parse.quote(str(version), safe="")

    url = PYPI_VERSION_API.format(
        encoded_name,
        encoded_version
    )

    return fetch_pypi_data(url)


# --------------------------------------------------
# VERSION SELECTION
# --------------------------------------------------

def find_matching_version(metadata, specifier):
    """Find the highest available version satisfying the constraint."""

    releases = metadata.get("releases", {})
    available_versions = []

    for version_string, files in releases.items():
        try:
            version = Version(version_string)

            if not specifier.contains(version, prereleases=True):
                continue

            # Ignore releases where all files are yanked
            if files and all(
                    file.get("yanked", False) for file in files
            ):
                continue

            available_versions.append(version)

        except InvalidVersion:
            continue

    if not available_versions:
        return None

    return max(available_versions)


# --------------------------------------------------
# PYTHON COMPATIBILITY
# --------------------------------------------------

def check_python_compatibility(requires_python):
    """
    Check Python 3.11 compatibility.

    Missing requires_python means compatible.
    Both lower and upper bounds are considered.
    """

    if not requires_python:
        return True

    try:
        specifier = SpecifierSet(requires_python)

        return specifier.contains(
            TARGET_PYTHON,
            prereleases=True
        )

    except Exception:
        # Do not incorrectly classify invalid metadata as compatible
        return False


# --------------------------------------------------
# HIGHEST PYTHON REQUIREMENT
# --------------------------------------------------

def get_python_minimum(requires_python):
    """
    Extract the strongest minimum Python version.

    Examples:
        >=3.9          -> (3.9, >=3.9)
        >=3.10,<3.13   -> (3.10, >=3.10)
        >3.11          -> (3.11, >3.11)
        >=3.9,>=3.12   -> (3.12, >=3.12)
    """

    if not requires_python:
        return None

    try:
        specifier = SpecifierSet(requires_python)

        lower_bounds = []

        for item in specifier:

            if item.operator in (">=", ">", "~="):

                version = Version(item.version)

                lower_bounds.append(
                    (version, item.operator)
                )

        if not lower_bounds:
            return None

        highest_version, operator = max(
            lower_bounds,
            key=lambda item: item[0]
        )

        return highest_version, operator

    except Exception:
        return None


# --------------------------------------------------
# CHECK INDIVIDUAL DEPENDENCY
# --------------------------------------------------

def check_dependency(requirement):

    package_name = requirement.name
    version_specifier = requirement.specifier

    result = {
        "package": package_name,
        "version": None,
        "requires_python": None,
        "status": None,
        "python_minimum": None
    }

    # Evaluate environment markers for Python 3.11
    if requirement.marker and not requirement.marker.evaluate({
        "python_version": "3.11",
        "python_full_version": "3.11.0"
    }):
        result["status"] = "SKIPPED"
        return result

    # 1. Check whether package exists
    metadata = get_package_metadata(package_name)

    if metadata is None:
        result["status"] = "NOT_FOUND"
        return result

    # 2. Select requested release
    if version_specifier:

        selected_version = find_matching_version(
            metadata,
            version_specifier
        )

    else:

        latest_version = metadata.get("info", {}).get("version")

        try:
            selected_version = (
                Version(latest_version)
                if latest_version else None
            )
        except InvalidVersion:
            selected_version = None

    if selected_version is None:
        result["status"] = "NOT_FOUND"
        return result

    # 3. Check exact release using version-specific API
    version_data = get_version_metadata(
        package_name,
        selected_version
    )

    if version_data is None:
        result["status"] = "NOT_FOUND"
        return result

    result["version"] = str(selected_version)

    # 4. Read requires_python
    info = version_data.get("info", {})
    requires_python = info.get("requires_python")

    result["requires_python"] = requires_python

    # 5. Check Python 3.11 compatibility
    if not check_python_compatibility(requires_python):
        result["status"] = "INCOMPATIBLE"
        return result

    result["status"] = "COMPATIBLE"

    # 6. Extract minimum Python requirement
    if requires_python:
        result["python_minimum"] = get_python_minimum(
            requires_python
        )

    return result


# --------------------------------------------------
# READ REQUIREMENTS FILE
# --------------------------------------------------

def read_requirements(filename):

    dependencies = []

    with open(filename, "r", encoding="utf-8") as file:

        for line in file:

            line = line.strip()

            if not line or line.startswith("#"):
                continue

            # Skip unsupported pip options and URLs
            if line.startswith(("-", "git+", "http:", "https:")):
                continue

            # Remove inline comments
            line = line.split(" #", 1)[0].strip()

            if not line:
                continue

            try:
                dependencies.append(Requirement(line))

            except Exception:
                continue

    return dependencies


# --------------------------------------------------
# MAIN
# --------------------------------------------------

def main():

    parser = argparse.ArgumentParser(
        description="Validate requirements against Python 3.11"
    )

    parser.add_argument(
        "file",
        help="Path to requirements.txt"
    )

    args = parser.parse_args()

    try:
        dependencies = read_requirements(args.file)

    except FileNotFoundError:
        print(f"Error: File not found: {args.file}")
        return

    results = []

    # Validate dependencies
    for dependency in dependencies:

        try:
            result = check_dependency(dependency)
            results.append(result)

        except Exception as error:
            raise RuntimeError(
                f"Failed to check {dependency.name}: {error}"
            ) from error

    # Calculate summary
    total = len(results)

    not_found = sum(
        result["status"] == "NOT_FOUND"
        for result in results
    )

    incompatible = sum(
        result["status"] == "INCOMPATIBLE"
        for result in results
    )

    # Find release with the highest minimum Python requirement
    candidates = []

    for result in results:

        if result["status"] != "COMPATIBLE":
            continue

        if result["python_minimum"] is None:
            continue

        minimum, operator = result["python_minimum"]

        candidates.append({
            "package": result["package"],
            "version": result["version"],
            "minimum": minimum,
            "operator": operator
        })

    highest_requirement = None

    if candidates:

        highest_requirement = max(
            candidates,
            key=lambda item: item["minimum"]
        )

    # Final output
    print(f"Dependencies Checked: {total}")
    print(f"Not found on PyPI: {not_found}")
    print(f"Incompatible with Python 3.11: {incompatible}")

    if highest_requirement:

        print(
            "Highest Requirement: "
            f"{highest_requirement['package']}=="
            f"{highest_requirement['version']} "
            f"({highest_requirement['operator']}"
            f"{highest_requirement['minimum']})"
        )

    else:

        print("Highest Requirement: None")


if __name__ == "__main__":

    try:
        main()

    except RuntimeError as error:
        print(f"Validation error: {error}")
        raise SystemExit(1)
