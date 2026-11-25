"""
Real tests for the List Operator node (harness.py's handle_list_operator),
built to match Dify's documented spec
(https://docs.dify.ai/en/cloud/use-dify/nodes/list-operator). See
GAP_REPORT §0.37.
"""
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.harness import handle_list_operator


def make_ctx(variables=None) -> dict:
    return {"input": "", "vars": dict(variables or {}), "output": None}


IMAGE = {"filename": "photo.jpg", "url": "http://127.0.0.1:8000/api/uploads/up-1/download", "content_type": "image/jpeg", "size": 204800}
IMAGE2 = {"filename": "diagram.png", "url": "http://127.0.0.1:8000/api/uploads/up-3/download", "content_type": "image/png", "size": 98000}
DOC = {"filename": "report.pdf", "url": "http://127.0.0.1:8000/api/uploads/up-2/download", "content_type": "application/pdf", "size": 512000}
DOC2 = {"filename": "notes.docx", "url": "http://127.0.0.1:8000/api/uploads/up-4/download", "content_type": "application/vnd.openxmlformats", "size": 30000}
REMOTE_IMAGE = {"filename": "external.jpg", "url": "https://cdn.example.com/external.jpg", "content_type": "image/jpeg", "size": 90000}


class TestRealNonArrayAndEmptyInputs:
    def test_a_non_array_input_produces_a_real_honest_empty_result_not_a_crash(self):
        ctx = make_ctx({"notarray": "oops"})
        result = handle_list_operator({"input_var": "{{notarray}}"}, ctx)
        assert result["vars"]["result"] == []
        assert result["vars"]["first_record"] is None
        assert result["vars"]["last_record"] is None

    def test_an_empty_array_produces_real_none_records(self):
        ctx = make_ctx({"empty": []})
        result = handle_list_operator({"input_var": "{{empty}}"}, ctx)
        assert result["vars"]["result"] == []
        assert result["vars"]["first_record"] is None
        assert result["vars"]["last_record"] is None


class TestRealPlainArrayFiltering:
    def test_greater_than_filters_a_real_number_array(self):
        ctx = make_ctx({"scores": [45, 90, 12, 78, 33, 99, 5]})
        cfg = {"input_var": "{{scores}}", "filters": [{"property": None, "operator": ">", "value": 20}]}
        result = handle_list_operator(cfg, ctx)
        assert result["vars"]["result"] == [45, 90, 78, 33, 99]

    def test_contains_filters_a_real_string_array(self):
        ctx = make_ctx({"names": ["Alice", "Bob", "Alicia", "Carol"]})
        cfg = {"input_var": "{{names}}", "filters": [{"property": None, "operator": "contains", "value": "Ali"}]}
        result = handle_list_operator(cfg, ctx)
        assert result["vars"]["result"] == ["Alice", "Alicia"]

    def test_is_not_excludes_a_real_exact_match(self):
        ctx = make_ctx({"items": ["a", "b", "c", "b"]})
        cfg = {"input_var": "{{items}}", "filters": [{"property": None, "operator": "is_not", "value": "b"}]}
        result = handle_list_operator(cfg, ctx)
        assert result["vars"]["result"] == ["a", "c"]

    def test_multiple_filters_are_real_and_semantics(self):
        ctx = make_ctx({"scores": [5, 15, 25, 35, 45]})
        cfg = {"input_var": "{{scores}}", "filters": [
            {"property": None, "operator": ">", "value": 10},
            {"property": None, "operator": "<", "value": 40},
        ]}
        result = handle_list_operator(cfg, ctx)
        assert result["vars"]["result"] == [15, 25, 35]


class TestRealFilePropertyFiltering:
    """The exact real "Mixed File Processing" pattern Dify's own docs
    describe: split a mixed array by type into separate real streams."""

    def test_type_image_filters_correctly(self):
        ctx = make_ctx({"uploads": [IMAGE, DOC, IMAGE2, DOC2]})
        cfg = {"input_var": "{{uploads}}", "filters": [{"property": "type", "operator": "is", "value": "image"}]}
        result = handle_list_operator(cfg, ctx)
        assert [f["filename"] for f in result["vars"]["result"]] == ["photo.jpg", "diagram.png"]

    def test_type_document_filters_correctly(self):
        ctx = make_ctx({"uploads": [IMAGE, DOC, IMAGE2, DOC2]})
        cfg = {"input_var": "{{uploads}}", "filters": [{"property": "type", "operator": "is", "value": "document"}]}
        result = handle_list_operator(cfg, ctx)
        assert [f["filename"] for f in result["vars"]["result"]] == ["report.pdf", "notes.docx"]

    def test_extension_filters_correctly(self):
        ctx = make_ctx({"uploads": [IMAGE, DOC]})
        cfg = {"input_var": "{{uploads}}", "filters": [{"property": "extension", "operator": "is", "value": "jpg"}]}
        result = handle_list_operator(cfg, ctx)
        assert [f["filename"] for f in result["vars"]["result"]] == ["photo.jpg"]

    def test_mime_type_filters_correctly(self):
        ctx = make_ctx({"uploads": [IMAGE, DOC]})
        cfg = {"input_var": "{{uploads}}", "filters": [{"property": "mime_type", "operator": "is", "value": "application/pdf"}]}
        result = handle_list_operator(cfg, ctx)
        assert [f["filename"] for f in result["vars"]["result"]] == ["report.pdf"]

    def test_size_comparison_filters_correctly(self):
        ctx = make_ctx({"uploads": [IMAGE, DOC, IMAGE2, DOC2]})
        cfg = {"input_var": "{{uploads}}", "filters": [{"property": "size", "operator": ">", "value": 100000}]}
        result = handle_list_operator(cfg, ctx)
        assert {f["filename"] for f in result["vars"]["result"]} == {"photo.jpg", "report.pdf"}

    def test_name_filters_correctly(self):
        ctx = make_ctx({"uploads": [IMAGE, DOC]})
        cfg = {"input_var": "{{uploads}}", "filters": [{"property": "name", "operator": "contains", "value": "report"}]}
        result = handle_list_operator(cfg, ctx)
        assert [f["filename"] for f in result["vars"]["result"]] == ["report.pdf"]

    def test_transfer_method_distinguishes_real_local_uploads_from_remote_urls(self):
        ctx = make_ctx({"uploads": [IMAGE, REMOTE_IMAGE]})
        cfg = {"input_var": "{{uploads}}", "filters": [{"property": "transfer_method", "operator": "is", "value": "local_file"}]}
        result = handle_list_operator(cfg, ctx)
        assert [f["filename"] for f in result["vars"]["result"]] == ["photo.jpg"]

    def test_a_combined_type_and_size_filter_matches_real_and_semantics_on_file_properties(self):
        ctx = make_ctx({"uploads": [IMAGE, DOC, IMAGE2, DOC2]})
        cfg = {"input_var": "{{uploads}}", "filters": [
            {"property": "type", "operator": "is", "value": "image"},
            {"property": "size", "operator": ">", "value": 100000},
        ]}
        result = handle_list_operator(cfg, ctx)
        assert [f["filename"] for f in result["vars"]["result"]] == ["photo.jpg"]


class TestRealSorting:
    def test_ascending_sort_on_a_real_plain_number_array(self):
        ctx = make_ctx({"nums": [5, 3, 9, 1]})
        cfg = {"input_var": "{{nums}}", "sort": {"property": None, "direction": "asc"}}
        result = handle_list_operator(cfg, ctx)
        assert result["vars"]["result"] == [1, 3, 5, 9]

    def test_descending_sort_on_a_real_plain_number_array(self):
        ctx = make_ctx({"nums": [5, 3, 9, 1]})
        cfg = {"input_var": "{{nums}}", "sort": {"property": None, "direction": "desc"}}
        result = handle_list_operator(cfg, ctx)
        assert result["vars"]["result"] == [9, 5, 3, 1]

    def test_sort_by_a_real_file_property(self):
        ctx = make_ctx({"uploads": [IMAGE, DOC, IMAGE2, DOC2]})
        cfg = {"input_var": "{{uploads}}", "sort": {"property": "size", "direction": "desc"}}
        result = handle_list_operator(cfg, ctx)
        assert result["vars"]["first_record"]["filename"] == "report.pdf"
        assert result["vars"]["first_record"]["size"] == 512000

    def test_items_missing_the_sort_property_sort_last_regardless_of_direction(self):
        ctx = make_ctx({"mixed": [{"size": 10}, {}, {"size": 5}]})
        cfg = {"input_var": "{{mixed}}", "sort": {"property": "size", "direction": "asc"}}
        result = handle_list_operator(cfg, ctx)
        assert result["vars"]["result"][-1] == {}  # the one with no real size value sorts last


class TestRealLimitAndRecords:
    def test_limit_clamps_to_the_real_documented_1_to_20_range(self):
        ctx = make_ctx({"nums": list(range(30))})
        cfg = {"input_var": "{{nums}}", "limit": 50}
        result = handle_list_operator(cfg, ctx)
        assert len(result["vars"]["result"]) == 20  # clamped to Dify's own documented max

    def test_limit_takes_the_real_first_n_after_filter_and_sort(self):
        ctx = make_ctx({"nums": [5, 3, 9, 1, 8, 2]})
        cfg = {"input_var": "{{nums}}", "sort": {"property": None, "direction": "desc"}, "limit": 3}
        result = handle_list_operator(cfg, ctx)
        assert result["vars"]["result"] == [9, 8, 5]

    def test_first_record_and_last_record_reflect_the_real_final_filtered_sorted_limited_array(self):
        ctx = make_ctx({"nums": [5, 3, 9, 1, 8, 2]})
        cfg = {"input_var": "{{nums}}", "filters": [{"property": None, "operator": ">", "value": 2}], "sort": {"property": None, "direction": "asc"}}
        result = handle_list_operator(cfg, ctx)
        assert result["vars"]["result"] == [3, 5, 8, 9]
        assert result["vars"]["first_record"] == 3
        assert result["vars"]["last_record"] == 9
