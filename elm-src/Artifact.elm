import List
import Dict
import String
import Basics
import Browser
import Browser.Navigation as Nav
import Html exposing (..)
import Html.Attributes exposing (..)
import Html.Events exposing (..)
import Http
import Json.Decode
import Json.Decode as Decode
import Json.Encode as Encode
import Url
import Url.Parser exposing (Parser, parse, query, (</>), (<?>), s)
import Url.Parser.Query exposing (map4, string)
import Url.Parser.Query as Query

import LibcflibRest exposing (Artifact, artifactDecoder)


-- Types

type alias Model =
    { error : Maybe Http.Error
    , response : Maybe Artifact
    , url : Url.Url
    , key : Nav.Key
    , query : Maybe UrlQuery
    }


initialModel : Url.Url -> Nav.Key -> Model
initialModel url key =
    Model
        Nothing
        Nothing
        url
        key
        (parseUrlQuery url)

--type Route
    --= ArtifactQuery UrlQuery


type alias UrlQuery =
    { pkg : Maybe String
    , channel : Maybe String
    , arch : Maybe String
    , name : Maybe String
    }



--routeParser : Parser (Route -> a) a
--routeParser =
--    map ArtifactQuery (s "artifact" <?> Query.string "pkg")

parseUrlQuery : Url.Url -> Maybe UrlQuery
parseUrlQuery url =
    parse (query (map4 UrlQuery
        (string "pkg")
        (string "channel")
        (string "arch")
        (string "name")
    )) url

type Msg
    = NoOp
    | LinkClicked Browser.UrlRequest
    | UrlChanged Url.Url
    | Response (Result Http.Error Artifact)



-- UPDATE


update : Msg -> Model -> ( Model, Cmd Msg )
update msg model =
    case msg of
        NoOp ->
            ( model, Cmd.none )

        LinkClicked urlRequest ->
            case urlRequest of
                Browser.Internal url ->
                    ( model, Nav.pushUrl model.key (Url.toString url) )

                Browser.External href ->
                    ( model, Nav.load href )

        UrlChanged url ->
            ( { model | url = url, query = (parseUrlQuery url) }, Cmd.none )

        Response (Ok response) ->
            ( { model | error = Nothing, response = Just response }, Cmd.none )

        Response (Err error) ->
            ( { model | error = Just error, response = Nothing }, Cmd.none )



-- HTTP

getArtifact : String -> String -> String -> String -> Cmd Msg
getArtifact pkg channel arch name =
  Http.get
    --{ url = "http://35.192.108.152/search?query=" ++ query ++ "&page_num=" ++ (String.fromInt page_num)
    { url = "http://localhost:8888/artifact?pkg=" ++ pkg ++ "&channel=" ++ channel ++ "&arch=" ++ arch ++ "&name=" ++ name
    , expect = Http.expectJson Response artifactDecoder
    }


-- VIEWS

viewArtifact : Artifact -> Html Msg
viewArtifact artifact =
    li []
        [ b [] [text (artifact.name ++ " v" ++ artifact.version)]
        , br [] []
        , text ("artifact: ")
        , i [] [text (artifact.spec.path)]
        ]


viewError : Http.Error -> Html Msg
viewError error =
    div [ class "error-container" ]
        [ h2 [] [ text "Artifact Errors" ]
        , div [] (case error of
            Http.BadUrl url ->
                [ text ("Bad URL: " ++ url)]
            Http.Timeout ->
                [ text "Network timeout" ]
            Http.NetworkError ->
                [ text "Network error" ]
            Http.BadStatus code ->
                [ text ("Bad status: " ++ String.fromInt code) ]
            Http.BadBody body ->
                [ text body ]
        )
        ]


viewBody :
    { a | error : Maybe Http.Error, response : Maybe Artifact }
    -> Html Msg
viewBody model =
    div []
        [ viewHeader
        , case model.response of
            Just response ->
                viewArtifact response

            Nothing ->
                text ""
        , case model.error of
            Just error ->
                viewError error

            Nothing ->
                text ""
        , viewFooter
        ]


viewHeader : Html msg
viewHeader =
    div [ class "header" ]
        [ h1 [] [ text ("Conda-Forge") ]
        ]

viewFooter : Html msg
viewFooter =
    div [ class "footer" ]
        [ a [ href "https://conda-forge.org/" ]
            [ text "[ homepage ] " ]
        , a [ href "https://conda-forge.org/docs/" ]
            [ text "[ docs ] " ]
        , a [ href "https://twitter.com/condaforge" ]
            [ text " [ twitter ]" ]
        , a [ href "https://www.flipcause.com/secure/cause_pdetails/NDA0OTk=" ]
            [ text "[ donate ] " ]
        ]

view : Model -> Browser.Document Msg
view model =
    Browser.Document
        "Conda-Forge Artifact"  -- title
        [viewBody model]       -- body


-- MAIN

init : Decode.Value -> Url.Url -> Nav.Key -> ( Model, Cmd Msg )
init flags url key =
    ( initialModel url key, Cmd.none  )


main : Program Decode.Value Model Msg
main =
  Browser.application
        { init = init
        , view = view
        , update = update
        , subscriptions = \_ -> Sub.none
        , onUrlChange = UrlChanged
        , onUrlRequest = LinkClicked
        }
